"""Shared plumbing for Atlas Watchtower: table, bucket, secrets, alerts, market clock.

Watchtower never touches the trading account. It only *listens*: the bot reports
heartbeats and closed trades, and Watchtower decides whether a human needs to know.
"""
import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from decimal import Decimal
from zoneinfo import ZoneInfo

import boto3

ET = ZoneInfo("America/New_York")

_ddb = boto3.resource("dynamodb")
_s3 = boto3.client("s3")
_sns = boto3.client("sns")
_ssm = boto3.client("ssm")
_cache = {}


def table():
    return _ddb.Table(os.environ["TABLE"])


def state_key(bot):
    """Every bot's live state sits in one small partition, so a sweep is one cheap Query - never a table Scan,
    which would read every heartbeat sample and trade too."""
    return {"pk": "STATES", "sk": bot}


def states():
    from boto3.dynamodb.conditions import Key
    return table().query(KeyConditionExpression=Key("pk").eq("STATES"))["Items"]


def bucket():
    return os.environ["BUCKET"]


def s3():
    return _s3


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def parse_ts(v):
    """Accept ISO-8601 strings or epoch seconds/milliseconds. Returns aware UTC datetime or None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float, Decimal)):
        v = float(v)
        if v > 1e12:  # milliseconds
            v /= 1000
        return dt.datetime.fromtimestamp(v, dt.timezone.utc)
    s = str(v).strip().replace("Z", "+00:00")
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def trading_day(t):
    """CME trading day: a session that opens Sunday 6pm ET belongs to Monday, and so on."""
    local = t.astimezone(ET)
    if local.hour >= 18:
        local += dt.timedelta(days=1)
    return local.date().isoformat()


def market_open(t):
    """CME Globex futures hours in ET: Sun 18:00 -> Fri 17:00, with a daily 17:00-18:00 halt.
    Holidays aren't modelled; a missed heartbeat on a holiday just means a quiet alert."""
    local = t.astimezone(ET)
    wd, h = local.weekday(), local.hour  # Mon=0 .. Sun=6
    if wd == 5:  # Saturday
        return False
    if wd == 6:  # Sunday
        return h >= 18
    if wd == 4:  # Friday
        return h < 17
    return h != 17  # Mon-Thu: everything except the 5pm hour


def secret(name_env):
    """Read a SecureString once per container and keep it warm."""
    name = os.environ.get(name_env)
    if not name:
        return None
    if name not in _cache:
        _cache[name] = _ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    return _cache[name]


def to_ddb(obj):
    return json.loads(json.dumps(obj), parse_float=Decimal)


def from_ddb(obj):
    if isinstance(obj, list):
        return [from_ddb(x) for x in obj]
    if isinstance(obj, dict):
        return {k: from_ddb(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    return obj


ALERT_TTL_DAYS = 30


def notify(subject, lines, bot=None, kind="info"):
    """Email always (SNS). Telegram too, when a bot token has been configured.
    Every alert is also kept for 30 days so the dashboard can show the history."""
    body = "\n".join(lines)
    t = now()
    try:
        table().put_item(Item={"pk": "ALERTS", "sk": f"{iso(t)}#{bot or '-'}#{kind}", "bot": bot or "-", "kind": kind,
                               "subject": subject, "body": body, "expires": int(t.timestamp()) + ALERT_TTL_DAYS * 86400})
    except Exception as e:  # the alert itself matters more than its history
        print(json.dumps({"alert_log_error": str(e)}))
    _sns.publish(TopicArn=os.environ["TOPIC_ARN"], Subject=subject[:100], Message=body)
    token, chat = secret("TELEGRAM_TOKEN_PARAM"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        data = urllib.parse.urlencode({"chat_id": chat, "text": f"{subject}\n\n{body}"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=5)
        except Exception as e:  # email already went out; never let Telegram break the watchdog
            print(json.dumps({"telegram_error": str(e)}))
    print(json.dumps({"alert": subject}))
