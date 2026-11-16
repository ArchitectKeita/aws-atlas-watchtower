"""Builds preview.html: the real dashboard + a fake backend, as one page you can open anywhere."""
import pathlib, re
root = pathlib.Path(__file__).resolve().parents[1]
page = (root / "src" / "index.html").read_text(encoding="utf-8")
mock = (root / "preview" / "mock.js").read_text(encoding="utf-8")
page = page.replace("<script>\n(() => {\n\"use strict\";", f"<script>\n{mock}\n</script>\n<script>\n(() => {{\n\"use strict\";", 1)
(root / "preview" / "preview.html").write_text(page, encoding="utf-8")
# artifact flavour: the host supplies doctype/html/head/body
body = re.sub(r"<!doctype html>\s*<html[^>]*>\s*<head>|</head>\s*<body>|</body>\s*</html>\s*$", "", page, flags=re.I)
(root / "preview" / "artifact.html").write_text(body.replace('<meta charset="utf-8">\n', "").replace(
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n', ""), encoding="utf-8")
print("ok")
