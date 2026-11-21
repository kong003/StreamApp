__version__ = "0.3.0"

try:  # wird beim Windows-Build mit dem Git-Commit erzeugt
    from ._build import BUILD
except ImportError:
    BUILD = "dev"
