import sys

if sys.argv[1:2] == ["reset-password"]:
    # Forgotten password. Run it from the server: docker exec linkarr python -m linkarr reset-password
    from . import auth
    from .config import load
    from .db import Database

    cfg = load()
    if not cfg.db_path.exists():
        sys.exit(f"No Linkarr database at {cfg.db_path}.")
    db = Database(cfg.db_path)
    code = auth.reset(db)
    db.close()
    print("The account was removed. Your libraries, settings and history are untouched.")
    print(f"Open Linkarr, make a new account and use this setup code: {code}")
else:
    from .main import main

    main()
