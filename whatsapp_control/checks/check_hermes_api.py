"""Check runtime imports against the upstream Hermes public API manifest."""

import ast
import json
from pathlib import Path
import sys


def check(root, allowed):
    findings = []
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            imports = []
            if isinstance(node, ast.Import):
                imports = [(alias.name, "") for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                imports = [(node.module, alias.name) for alias in node.names]
            for module, name in imports:
                if module.split(".")[0] in sys.stdlib_module_names | {"aiohttp"}:
                    continue
                if name != "*" and (f"{module}:{name}" in allowed or
                                    (module in allowed and not name.startswith("_"))):
                    continue
                findings.append(f"{path.name}:{node.lineno}: {module}{':' + name if name else ''}")
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "ctx" and node.attr.startswith("_")):
                findings.append(f"{path.name}:{node.lineno}: ctx.{node.attr}")
    return findings


if __name__ == "__main__":
    manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    public = {name for group in manifest["groups"] for name in group["entries"]}
    findings = check(Path(__file__).resolve().parents[1], public)
    if findings:
        sys.exit("Imports or context attributes outside the Hermes public API:\n" + "\n".join(findings))
    print("Hermes public API imports verified")
