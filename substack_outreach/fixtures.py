"""One confirmed recipient, three labelled variants; never fetched or generalized."""
from .common import FITS, dumps, html_email
from .evidence import BASE_CLAIMS
from .sources import save_articles

ARTICLE = "https://substack.com/@alreadydawn/p-197268804"
DETAILS = [
    ("batching messages", "batching messages at your computer to minimize context switching",
     "made me think about which digital tasks we might delegate to AI while keeping control of the decisions.",
     "Would you be interested in exploring that distinction with us?"),
    ("digital habits", "removing Twitter from your phone to reduce mindless scrolling",
     "made me think about whether some routine digital actions are worth delegating, and which ones we would keep doing ourselves.",
     "Would you be interested in exploring that question with us?"),
    ("new commitments", "being careful about taking on new commitments",
     "made me think about the boundary between delegating a digital task and retaining the decision to take it on.",
     "Would you be interested in discussing that boundary with us?")]


def add_fixture(pipeline):
    store = pipeline.store
    row = store.db.execute("SELECT publication_id FROM aliases WHERE key='fixture:alreadydawn'").fetchone()
    if row:
        return row[0]
    data = {"publication": "Alan / alreadydawn — user-supplied fixture (publication unresolved)",
            "publication_url": "", "profile_url": "https://substack.com/@alreadydawn", "author_name": "Alan",
            "confirmed_first_name": "Alan", "name_source": "User-confirmed public first name in task instructions",
            "fit_category": FITS[2], "fit_reasons": ["Exploratory editorial angle supplied by the user; no evidence that this author covers AI."],
            "owner": "", "contact_route": "", "contact_source_url": "", "contact_kind": "", "contact_confirmed": False,
            "source": {}, "source_kind": "user_fixture", "research": {}, "import_flags": [], "skip_reason": ""}
    cur = store.db.execute("INSERT INTO publications(data,score,author_key,status) VALUES(?,?,?,?)",
                           (dumps(data), 1, "profile:https://substack.com/@alreadydawn", "Researched"))
    pid = cur.lastrowid
    store.db.execute("INSERT INTO aliases VALUES('fixture:alreadydawn',?)", (pid,))
    store.db.commit()
    pub = store.get(pid)
    # A normalized rendering of user-supplied details, not a quotation attributed to a fetched page.
    text = "User-supplied summary, not fetched article text:\n" + "\n".join(d[1] for d in DETAILS)
    save_articles(store, pub, [{"title": "Post on staying grounded (user-supplied description)", "url": ARTICLE,
                    "date": None, "byline": "Alan (user confirmed)", "text": text, "content_status": "excerpt",
                    "source_kind": "user_fixture", "source_url": ARTICLE,
                    "permission_basis": "User supplied these details for this labelled test fixture",
                    "membership_confirmed": False, "relationship_source": "User supplied profile and article; publication unresolved",
                    "flags": ["NOT_FETCHED", "USER_SUMMARY_NOT_AUTHOR_QUOTATION"]}], supplied=True)
    set_variant(pipeline, pid, 0)
    return pid


def set_variant(pipeline, pid, variant):
    pub = pipeline.store.get(pid)
    topic, detail, connection, ask = DETAILS[variant]
    e = {"author_name": "Alan", "name_source": pub["name_source"], "publication": pub["publication"],
         "publication_url": "", "fit_category": FITS[2], "article_title": pub["articles"][0]["title"],
         "article_url": ARTICLE, "article_date": "", "specific_point": detail, "supporting_excerpt": detail,
         "drex_connection": connection, "selected_claim_ids": BASE_CLAIMS, "suggested_ask": ask,
         "contact_route": "", "contact_source_url": "", "ready_to_draft": True,
         "review_flags": ["FIXTURE_ONLY_USER_SUPPLIED_NOT_FETCHED"], "evidence_reviewed_by": "User-supplied fixture only",
         "ask_format": "exploratory discussion", "email_detail": detail,
         "mock_opening": f"Your post ({ARTICLE})—especially {detail}—{connection}",
         "mock_subject": f"Your post on {topic} × Drex Max"}
    pipeline.update(pid, {"evidence": e})
    return e


def previews(pipeline):
    from .models import MockWriter, writing_input
    from .evidence import style_checks
    pid = add_fixture(pipeline)
    pub = pipeline.store.get(pid)
    original = pub["evidence"]
    facts, _ = pipeline.facts()
    results = []
    # Previews are separate artifacts; do not overwrite any saved/edited draft.
    for i, (topic, detail, connection, ask) in enumerate(DETAILS):
        e = dict(original, email_detail=detail, supporting_excerpt=detail, specific_point=detail,
                 suggested_ask=ask, drex_connection=connection,
                 mock_opening=f"Your post ({ARTICLE})—especially {detail}—{connection}",
                 mock_subject=f"Your post on {topic} × Drex Max")
        draft = MockWriter().generate(writing_input(pub, e, facts, pipeline.config["sender"]), e)
        results.append({"label": f"MOCK PREVIEW {i+1}: Alan fixture variant; user-supplied evidence, not fetched; pending product/offer approval; not send-ready",
                        **draft, "html_body": html_email(draft['plain_text_body']),
                        "checks": style_checks(draft["plain_text_body"], draft["subject"], e, pipeline.config["sender"])})
    return results
