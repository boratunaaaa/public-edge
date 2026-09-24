import logging
import sys


def setup(level: str = "INFO") -> None:
    logging.basicConfig(stream=sys.stdout, level=level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    for noisy in ("httpx", "httpcore", "pdfminer", "pdfplumber"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
