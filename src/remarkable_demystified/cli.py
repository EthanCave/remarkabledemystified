"""``rmd``: a command-line client for your reMarkable, and the API server launcher.

Examples::

    rmd ls /Books
    rmd put paper.pdf --to /Papers
    rmd get "/Notes/Meeting" -o meeting.pdf
    rmd text "/Notes/Meeting"
    rmd render "/Notes/Meeting" --page 1 -o page.png
    rmd note "Shopping" --text "# Shopping\\n- [ ] eggs"
    rmd serve --port 8000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__
from .config import BACKENDS, connect, load_settings, read_config_file
from .errors import InvalidRequest, NotFound, RemarkableError
from .library import Library
from .model import Item


def _connect(args: argparse.Namespace) -> Library:
    overrides = {
        "backend": args.backend,
        "ssh_host": args.host,
        "ssh_password": args.password,
        "ssh_key": args.key,
        "local_dir": args.local_dir,
    }
    settings = load_settings(args.config, **overrides)
    return connect(settings)


def _fmt_item(item: Item, long: bool) -> str:
    marker = "/" if item.is_folder else ""
    if not long:
        return item.name + marker
    kind = "dir " if item.is_folder else (item.file_type or "doc")[:4].ljust(4)
    pages = f"{item.page_count:>4}p" if item.page_count else "     "
    when = item.last_modified.strftime("%Y-%m-%d %H:%M") if item.last_modified else " " * 16
    star = "*" if item.pinned else " "
    return f"{kind} {pages} {when} {star} {item.name}{marker}  [{item.id}]"


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def cmd_ls(lib: Library, args: argparse.Namespace) -> None:
    target = lib.resolve(args.path)
    items = lib.children(target.id) if target.is_folder else [target]
    if args.json:
        _print_json([i.to_dict() for i in items])
        return
    for item in items:
        print(_fmt_item(item, args.long))


def cmd_tree(lib: Library, args: argparse.Namespace) -> None:
    root = lib.resolve(args.path)

    def show(nodes: list[dict[str, Any]], prefix: str) -> None:
        for i, node in enumerate(nodes):
            last = i == len(nodes) - 1
            item = node["item"]
            print(prefix + ("└── " if last else "├── ") + item["name"] + ("/" if item["type"] == "folder" else ""))
            if node.get("children"):
                show(node["children"], prefix + ("    " if last else "│   "))

    print(lib.path(root.id))
    show(lib.tree(root.id), "")


def cmd_info(lib: Library, args: argparse.Namespace) -> None:
    _print_json(lib.device_info())


def _output_path(args: argparse.Namespace, item: Item, ext: str) -> Path:
    if args.output:
        out = Path(args.output)
        return out / f"{item.name}.{ext}" if out.is_dir() else out
    safe = "".join(c if c not in '/\\:*?"<>|' else "_" for c in item.name)
    return Path(f"{safe}.{ext}")


def cmd_get(lib: Library, args: argparse.Namespace) -> None:
    item = lib.resolve(args.path)
    if item.is_folder:
        raise InvalidRequest(f"{args.path} is a folder")
    if args.format == "original":
        data, ext = lib.source(item.id)
    elif args.format == "rmdoc":
        data, ext = lib.export_archive(item.id), "rmdoc"
    else:
        data, ext = lib.export_pdf(item.id), "pdf"
    out = _output_path(args, item, ext)
    if str(out) == "-":
        sys.stdout.buffer.write(data)
        return
    out.write_bytes(data)
    print(f"saved {out} ({len(data) // 1024} KiB)", file=sys.stderr)


def cmd_put(lib: Library, args: argparse.Namespace) -> None:
    parent = lib.ensure_folder(args.to) if args.to and args.mkdir else lib.resolve(args.to or "/")
    if not parent.is_folder:
        raise InvalidRequest(f"{args.to} is not a folder")
    if args.name and len(args.files) > 1:
        raise InvalidRequest("--name only works with a single file")
    for path in args.files:
        data = Path(path).read_bytes()
        item = lib.upload(data, Path(path).name, parent=parent.id, name=args.name)
        print(f"uploaded {lib.path(item.id)}  [{item.id}]")


def cmd_mkdir(lib: Library, args: argparse.Namespace) -> None:
    folder = lib.ensure_folder(args.path)
    print(f"{lib.path(folder.id)}  [{folder.id}]")


def cmd_mv(lib: Library, args: argparse.Namespace) -> None:
    item = lib.resolve(args.source)
    try:
        dest = lib.resolve(args.dest)
    except NotFound:
        dest = None
    if dest is not None and dest.is_folder:
        moved = lib.move(item.id, dest.id)
    else:
        target = PurePosixPath(args.dest)
        parent = lib.resolve(str(target.parent)) if str(target.parent) not in ("", ".") else lib.get(item.parent)
        if not parent.is_folder:
            raise InvalidRequest(f"{target.parent} is not a folder")
        moved = lib.update(item.id, name=target.name, parent=parent.id)
    print(lib.path(moved.id))


def cmd_rm(lib: Library, args: argparse.Namespace) -> None:
    for path in args.paths:
        item = lib.resolve(path)
        lib.delete(item.id, permanent=args.permanent, recursive=args.recursive)
        print(("deleted " if args.permanent else "trashed ") + path)


def cmd_restore(lib: Library, args: argparse.Namespace) -> None:
    item = lib.resolve(args.path)
    dest = lib.resolve(args.to) if args.to else None
    print(lib.path(lib.restore(item.id, dest.id if dest else "").id))


def cmd_empty_trash(lib: Library, args: argparse.Namespace) -> None:
    print(f"deleted {lib.empty_trash()} item(s)")


def cmd_pin(lib: Library, args: argparse.Namespace) -> None:
    lib.update(lib.resolve(args.path).id, pinned=not args.unpin)


def cmd_tag(lib: Library, args: argparse.Namespace) -> None:
    item = lib.resolve(args.path)
    if args.tags or args.clear:
        item = lib.update(item.id, tags=[] if args.clear else args.tags)
    print(", ".join(item.tags))


def cmd_text(lib: Library, args: argparse.Namespace) -> None:
    doc = lib.text(lib.resolve(args.path).id)
    if args.json:
        _print_json(doc.to_dict())
    else:
        print(doc.to_markdown())


def cmd_pages(lib: Library, args: argparse.Namespace) -> None:
    for page in lib.pages(lib.resolve(args.path).id):
        src = f"pdf p.{page.source_page + 1}" if page.source_page is not None else "inserted" if page.template is None else ""
        print(f"{page.index + 1:>4}  {'annotated' if page.has_annotations else '         '}  {src}")


def cmd_render(lib: Library, args: argparse.Namespace) -> None:
    item = lib.resolve(args.path)
    data, _media = lib.render_page(item.id, args.page - 1, args.format, scale=args.scale)
    out = Path(args.output) if args.output else Path(f"{item.name}-p{args.page}.{args.format}")
    out.write_bytes(data)
    print(f"saved {out}", file=sys.stderr)


def cmd_note(lib: Library, args: argparse.Namespace) -> None:
    text = args.text.replace("\\n", "\n") if args.text else None
    if args.file:
        text = Path(args.file).read_text() if args.file != "-" else sys.stdin.read()
    parent = lib.ensure_folder(args.to) if args.to else lib.resolve("/")
    item = lib.create_notebook(args.name, parent.id, text=text)
    print(f"created {lib.path(item.id)}  [{item.id}]")


def cmd_restart_ui(lib: Library, args: argparse.Namespace) -> None:
    lib.restart_ui()


def cmd_cloud_login(args: argparse.Namespace) -> None:
    from .storage.cloud import CONNECT_URL, CloudAuth

    settings = load_settings(args.config)
    code = args.code or input(f"One-time code from {CONNECT_URL}: ")
    auth = CloudAuth(settings.cloud_token_file, auth_host=settings.cloud_auth_host)
    auth.register(code)
    auth.user_token()
    print(f"Registered. Tokens saved to {settings.cloud_token_file}. Use --backend cloud or RMD_BACKEND=cloud.")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .server.app import create_app

    settings = load_settings(
        args.config, backend=args.backend, ssh_host=args.host, ssh_password=args.password,
        ssh_key=args.key, local_dir=args.local_dir, api_token=args.token,
    )
    if args.bind not in ("127.0.0.1", "localhost", "::1") and not settings.api_token:
        print("warning: serving on a public interface without an API token (set RMD_API_TOKEN or --token);"
              " anyone on your network could read and delete your documents.", file=sys.stderr)
    app = create_app(settings=settings)
    uvicorn.run(app, host=args.bind, port=args.port, log_level="info")


def _common_options(default: object) -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--backend", choices=BACKENDS, default=default, help="how to reach the tablet (default: ssh)")
    common.add_argument("--host", default=default, help="tablet address for SSH (default 10.11.99.1 = USB cable)")
    common.add_argument("--password", default=default, help="SSH root password (or RMD_SSH_PASSWORD)")
    common.add_argument("--key", default=default, help="SSH private key file")
    common.add_argument("--local-dir", default=default, help="xochitl directory for --backend local")
    common.add_argument("--config", default=default, help="config file (default ~/.config/remarkable-demystified/config.toml)")
    return common


def _mcp_client_config(args: argparse.Namespace, settings) -> dict[str, Any]:
    """A ready-to-paste claude_desktop_config.json entry for this machine."""
    import shutil

    exe = shutil.which("rmd")
    command, cli_args = (exe, ["mcp"]) if exe else (sys.executable, ["-m", "remarkable_demystified", "mcp"])
    if args.allow_write:
        cli_args.append("--allow-write")
    env: dict[str, str] = {"RMD_BACKEND": settings.backend}
    if settings.backend == "ssh":
        if settings.ssh_host != "10.11.99.1":
            env["RMD_SSH_HOST"] = settings.ssh_host
        if settings.ssh_key:
            env["RMD_SSH_KEY"] = settings.ssh_key
        elif not (read_config_file(args.config).get("ssh") or {}).get("password"):
            # Claude launches rmd with only this environment, so the password must come
            # from here or the config file. Never print the real one.
            env["RMD_SSH_PASSWORD"] = "<root password: Settings > Help > Copyrights and licenses>"
    elif settings.backend == "local" and settings.local_dir:
        env["RMD_LOCAL_DIR"] = str(Path(settings.local_dir).expanduser().resolve())
    elif settings.backend == "usb" and settings.usb_url != "http://10.11.99.1":
        env["RMD_USB_URL"] = settings.usb_url
    if args.config:
        env["RMD_CONFIG"] = str(Path(args.config).expanduser().resolve())
    return {"mcpServers": {"remarkable": {"command": command, "args": cli_args, "env": env}}}


def cmd_mcp(args: argparse.Namespace) -> None:
    settings = load_settings(
        args.config, backend=args.backend, ssh_host=args.host, ssh_password=args.password,
        ssh_key=args.key, local_dir=args.local_dir,
    )
    if args.print_config:
        _print_json(_mcp_client_config(args, settings))
        return
    try:
        from .server.mcp_server import build_server
    except ImportError as exc:
        raise InvalidRequest(
            f"the MCP server needs the optional extra: pip install 'remarkable-demystified[mcp]' ({exc})"
        ) from exc
    from .library import ALL_CAPABILITIES
    from .storage.usbweb import UsbWebLibrary

    caps = UsbWebLibrary.capabilities if settings.backend == "usb" else ALL_CAPABILITIES
    server = build_server(
        lambda: connect(settings),
        allow_write=args.allow_write,
        capabilities=caps,
        max_upload_mb=settings.max_upload_mb,
    )
    # stdio transport: stdout belongs to the protocol, so nothing else may print to it.
    server.run()


def build_parser() -> argparse.ArgumentParser:
    # Options work before or after the subcommand; SUPPRESS keeps the subcommand's
    # copy from overwriting a value given before it.
    common = _common_options(argparse.SUPPRESS)
    parser = argparse.ArgumentParser(
        prog="rmd", description="Read and edit your reMarkable.", parents=[_common_options(None)]
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, func, help: str, **kw):
        p = sub.add_parser(name, help=help, parents=[common], **kw)
        p.set_defaults(func=func)
        return p

    p = add("ls", cmd_ls, "list a folder")
    p.add_argument("path", nargs="?", default="/")
    p.add_argument("-l", "--long", action="store_true")
    p.add_argument("--json", action="store_true")

    p = add("tree", cmd_tree, "show the folder tree")
    p.add_argument("path", nargs="?", default="/")

    add("info", cmd_info, "device / backend information")

    p = add("get", cmd_get, "download a document (annotated PDF by default)")
    p.add_argument("path")
    p.add_argument("-o", "--output", help="file or directory ('-' for stdout)")
    p.add_argument("-f", "--format", choices=("pdf", "original", "rmdoc"), default="pdf")

    p = add("put", cmd_put, "upload PDFs, EPUBs or .rmdoc files")
    p.add_argument("files", nargs="+")
    p.add_argument("--to", help="destination folder path")
    p.add_argument("--mkdir", action="store_true", help="create the destination folder if missing")
    p.add_argument("--name", help="visible name on the tablet")

    p = add("mkdir", cmd_mkdir, "create a folder (and missing parents)")
    p.add_argument("path")

    p = add("mv", cmd_mv, "move and/or rename")
    p.add_argument("source")
    p.add_argument("dest", help="existing folder, or new path/name")

    p = add("rm", cmd_rm, "move to trash (or delete with --permanent)")
    p.add_argument("paths", nargs="+")
    p.add_argument("--permanent", action="store_true")
    p.add_argument("-r", "--recursive", action="store_true")

    p = add("restore", cmd_restore, "restore an item from the trash")
    p.add_argument("path", help="e.g. /Trash/Old notes")
    p.add_argument("--to", help="destination folder (default: My files)")

    add("empty-trash", cmd_empty_trash, "permanently delete everything in the trash")

    p = add("pin", cmd_pin, "favorite a document or folder")
    p.add_argument("path")
    p.add_argument("--unpin", action="store_true")

    p = add("tag", cmd_tag, "show or set tags")
    p.add_argument("path")
    p.add_argument("tags", nargs="*")
    p.add_argument("--clear", action="store_true")

    p = add("text", cmd_text, "typed text and highlights as Markdown")
    p.add_argument("path")
    p.add_argument("--json", action="store_true")

    p = add("pages", cmd_pages, "list a document's pages")
    p.add_argument("path")

    p = add("render", cmd_render, "render one page to PNG/SVG/PDF")
    p.add_argument("path")
    p.add_argument("--page", type=int, default=1, help="1-based page number")
    p.add_argument("-f", "--format", choices=("png", "svg", "pdf"), default="png")
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("-o", "--output")

    p = add("note", cmd_note, "create a notebook, optionally with typed text")
    p.add_argument("name")
    p.add_argument("--text", help="Markdown-ish text ('\\n' for new lines)")
    p.add_argument("--file", help="read the text from a file ('-' for stdin)")
    p.add_argument("--to", help="folder path (created if missing)")

    add("restart-ui", cmd_restart_ui, "restart the tablet UI (SSH)")

    p = sub.add_parser("cloud-login", help="connect to your reMarkable cloud account", parents=[common])
    p.add_argument("code", nargs="?", help="8-letter code from my.remarkable.com/device/browser/connect")
    p.set_defaults(func=None, standalone=cmd_cloud_login)

    p = sub.add_parser("mcp", help="run the MCP server for Claude Desktop / Claude Code (stdio)", parents=[common])
    p.add_argument("--allow-write", action="store_true",
                   help="also offer tools that create, rename, move, upload and trash (never permanent delete)")
    p.add_argument("--print-config", action="store_true",
                   help="print a claude_desktop_config.json entry for this machine and exit")
    p.set_defaults(func=None, standalone=cmd_mcp)

    p = sub.add_parser("serve", help="run the REST API", parents=[common])
    p.add_argument("--bind", default="127.0.0.1", help="interface to listen on (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=int(os.environ.get("RMD_PORT", "8000")))
    p.add_argument("--token", help="require this API token (or RMD_API_TOKEN)")
    p.set_defaults(func=None, standalone=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.func is None:
            args.standalone(args)
            return 0
        with _connect(args) as lib:
            args.func(lib, args)
        return 0
    except RemarkableError as exc:
        print(f"rmd: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
