import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from streamlit.testing.v1 import AppTest
from tests.test_pipeline import make
from pcdn import qa, readers
from pcdn.config import load_domains
from pcdn.sources import build_sources, fetch
from pcdn.preprocess import PreOpts, preprocess_all

td = Path(tempfile.mkdtemp()); make(td)
doms = load_domains()
srcs = build_sources([(str(p.relative_to(td)), p.stat().st_size) for p in td.rglob("*") if p.is_file()])
res = [r for s in srcs for r in fetch(s, Path(tempfile.mkdtemp()), None, td)]
raw, struct = readers.ingest(res, lambda m: None)
ir = qa.run_all_qa(raw, doms, struct)
clean, rej = preprocess_all({k: v.copy() for k, v in raw.items()}, doms, PreOpts(), lambda m: None)
ic = qa.run_all_qa(clean, doms); clean = qa.apply_status(clean, ic)

at = AppTest.from_file("../app.py", default_timeout=90)
at.session_state["raw"], at.session_state["struct"], at.session_state["issues_raw"] = raw, struct, ir
at.run(); assert not at.exception, at.exception
print("raw OK")
at.session_state["clean"], at.session_state["issues_clean"], at.session_state["rejected"] = clean, ic, rej
at.run(); assert not at.exception, at.exception
print("clean OK")
btn = [b for b in at.button if "Générer" in b.label][0]; btn.click(); at.run()
assert not at.exception, at.exception
print("export OK", [d for d in at.session_state["exports"]])
