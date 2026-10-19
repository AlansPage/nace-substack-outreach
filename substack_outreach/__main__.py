import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .fixtures import add_fixture, previews
from .pipeline import Pipeline
from .sources import save_articles


def main():
    parser = argparse.ArgumentParser(description="Local Substack research and editorial drafts. No sending.")
    parser.add_argument("--config", help="Local JSON configuration; paths are relative to the current working directory")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("import", "pilot"):
        p = subs.add_parser(name)
        p.add_argument("--input")
        p.add_argument("--batch", type=int, default=None)
        p.add_argument("--shortlist", type=int, default=None)
    p = subs.add_parser("process")
    p.add_argument("--batch", type=int)
    p.add_argument("--collect", action="store_true", help="Collect only permission-approved publisher feeds")
    p = subs.add_parser("collect-contacts", help="Propose contact routes from pilot publications' about pages")
    p.add_argument("--batch", type=int)
    p.add_argument("--retry-failed", action="store_true", help="Try about pages that failed before")
    p = subs.add_parser("research-import")
    p.add_argument("file", help="JSON object {publication_id, articles: [...]} or list of those objects")
    p = subs.add_parser("contact-import")
    p.add_argument("file", help="JSON list of explicit publication_id, channel, source, event and optional identity_alias")
    p = subs.add_parser("generate")
    p.add_argument("id", type=int)
    p.add_argument("--regenerate", action="store_true")
    p = subs.add_parser("show")
    p.add_argument("id", type=int)
    p = subs.add_parser("update")
    p.add_argument("id", type=int)
    p.add_argument("file", help="JSON publication fields/evidence/permission configuration")
    p = subs.add_parser("export")
    p.add_argument("--format", choices=["csv", "json"], default="json")
    p.add_argument("--scope", choices=["pilot", "shortlisted", "all"], default="pilot")
    p.add_argument("--out", required=True)
    p = subs.add_parser("serve")
    p.add_argument("--port", type=int, default=8765)
    subs.add_parser("metrics")
    subs.add_parser("fixture")
    p = subs.add_parser("previews")
    p.add_argument("--out", default=".local/substack/fixture-previews.json")
    args = parser.parse_args()
    # Results are printed as UTF-8 JSON whatever the console or redirect target uses.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        parser.exit(2, f"Error: config file not found: {args.config}\n")
    except json.JSONDecodeError as exc:
        parser.exit(2, f"Error: config is not valid JSON ({exc})\n")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    if getattr(args, "batch", None) is not None and args.batch <= 0:
        parser.error("--batch must be positive")
    if getattr(args, "shortlist", None) is not None and args.shortlist <= 0:
        parser.error("--shortlist must be positive")
    if args.command == "serve":
        from .server import serve
        serve(config, args.port)
        return
    pipeline = Pipeline(config)
    try:
        if args.command == "pilot":
            result = pipeline.pilot(args.input, args.batch, args.shortlist)
        elif args.command == "import":
            result = pipeline.import_workbook(args.input, args.batch, args.shortlist)
        elif args.command == "process":
            result = pipeline.process(args.batch, args.collect)
        elif args.command == "collect-contacts":
            result = pipeline.collect_contacts(args.batch, args.retry_failed)
        elif args.command == "generate":
            result = pipeline.generate(args.id, args.regenerate)
        elif args.command == "show":
            result = pipeline.detail(args.id)
        elif args.command == "update":
            result = pipeline.update(args.id, json.loads(Path(args.file).read_text(encoding="utf-8-sig")))
        elif args.command == "research-import":
            records = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
            records = records if isinstance(records, list) else [records]
            for item in records:
                save_articles(pipeline.store, pipeline.store.get(item["publication_id"]), item["articles"], supplied=True)
            result = {"research_imports": len(records)}
        elif args.command == "contact-import":
            records = json.loads(Path(args.file).read_text(encoding="utf-8-sig"))
            for item in records:
                pipeline.action(item["publication_id"], "history", item)
            result = {"contact_records": len(records)}
        elif args.command == "fixture":
            pid = add_fixture(pipeline)
            result = pipeline.generate(pid)
        elif args.command == "previews":
            result = previews(pipeline)
            write_private(args.out, json.dumps(result, ensure_ascii=False, indent=2))
            import html
            rendered = '<!doctype html><meta charset="utf-8"><title>Alan fixture draft previews</title><style>body{font:16px/1.65 system-ui;max-width:780px;margin:45px auto;padding:0 24px;color:#243b30}article{border-top:1px solid #cbd7d0;padding:24px 0}.flag{color:#95601b;font-size:13px}a{color:#256849}h2{font-size:21px}</style><h1>Three Alan fixture variants</h1><p>User-supplied evidence only. These are not three researched publications. Product facts and offers remain pending approval; none is send-ready.</p>'
            for draft in result:
                rendered += '<article><p class="flag">' + html.escape(draft['label']) + '</p><h2>' + html.escape(draft['subject']) + '</h2><p class="flag">' + str(draft['checks']['body_word_count']) + ' body words</p>' + draft['html_body'] + '</article>'
            write_private(str(Path(args.out).with_suffix('.html')), rendered)
        elif args.command == "export":
            write_private(args.out, pipeline.export(args.format, args.scope))
            result = {"exported": args.out}
        else:
            result = pipeline.store.metrics()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    finally:
        pipeline.store.close()


def write_private(path, text):
    p = Path(path).resolve()
    public = (Path(__file__).resolve().parent.parent / "public").resolve()
    if p == public or public in p.parents:
        raise ValueError("Private exports cannot be written into public static assets")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
