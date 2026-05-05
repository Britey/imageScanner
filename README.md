# imgdupe

Local exact and near-duplicate image search using SQLite and perceptual hashes.

## Current MVP slice

- Recursive image scan
- SQLite schema
- Exact SHA-256 file hash
- pHash256, wHash256, dHash256
- 3x3 regional pHash grid
- SQLite hash band index
- Query-by-image with ranked scores
- Optional HTML query report

## Commands

```powershell
imgdupe scan C:\path\to\images --db index.sqlite
imgdupe query C:\path\to\image.jpg --db index.sqlite --html result.html
```

Until the venv is repaired, run through the module form once Python works:

```powershell
.\.venv\Scripts\python.exe -m imgdupe.cli scan C:\path\to\images --db index.sqlite
```

## Notes

The project stores paths and metadata only. It never modifies or deletes original images.
