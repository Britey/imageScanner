# imgdupe

Local image search using SQLite and perceptual hashes.

## Current MVP slice

- Recursive image scan
- SQLite schema
- Exact SHA-256 file hash
- pHash256, wHash256, dHash256
- 3x3 regional pHash grid
- SQLite hash band index
- Query-by-image with ranked scores
- Optional HTML query report
- Similar-image grouping
- Static HTML cluster review gallery
- Local web UI for query-by-image search

## Commands

```powershell
imgdupe scan C:\path\to\images --db index.sqlite
imgdupe query C:\path\to\image.jpg --db index.sqlite --html result.html --min-score 55
imgdupe cluster --db index.sqlite --min-score 70
imgdupe review --db index.sqlite --out review
imgdupe serve --db index.sqlite
imgdupe failures --db index.sqlite
```

If the console script is not installed yet, run through the module form:

```powershell
.\.venv\Scripts\python.exe -m imgdupe.cli scan C:\path\to\images --db index.sqlite
```

## Notes

The project stores paths and metadata only. It never modifies or deletes original images.
