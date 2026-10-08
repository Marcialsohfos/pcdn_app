import sys, tempfile, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer
from tests.test_pipeline import make
from pcdn import readers
from pcdn.sources import FtpClient, build_sources, fetch

root = Path(tempfile.mkdtemp()); make(root / "ISSEA" / "export")
a = DummyAuthorizer(); a.add_user("iss", "pw", str(root), perm="elr")
H = FTPHandler; H.authorizer = a
srv = FTPServer(("127.0.0.1", 2121), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
with FtpClient("127.0.0.1", "iss", "pw", 2121, tls=False) as c:
    ent = c.walk("/", 5); print(len(ent), "fichiers")
    srcs = build_sources(ent); print([(s.path, s.layer, s.zone) for s in srcs])
    res = [r for s in srcs for r in fetch(s, Path(tempfile.mkdtemp()), c)]
layers, st = readers.ingest(res, print)
print({k: len(v) for k, v in layers.items()}); srv.close_all()
try:
    FtpClient("11.111.427.41", "iss", "pw", timeout=3).connect()
except ConnectionError as e: print("Erreur attendue:", str(e)[:90])
