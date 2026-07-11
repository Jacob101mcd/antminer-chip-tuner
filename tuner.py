"""Antminer Chip Tuner — entry point shim. Real package lives in tuner_app/.

This file exists so `python3 tuner.py` (the historical command) keeps
working. The package is at tuner_app/; entry point is tuner_app.main.main().
"""

from tuner_app.main import main

if __name__ == "__main__":
    main()
