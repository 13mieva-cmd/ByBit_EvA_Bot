"""Compatibility shim — redirects to scanner.py (Railway old entrypoint)."""
from scanner import *  # noqa: F401,F403

if __name__ == "__main__":
    from scanner import main
    import asyncio
    asyncio.run(main())
