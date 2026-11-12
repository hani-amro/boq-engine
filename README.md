# boq-engine

A deterministic bill-of-quantities engine for interior finishing works, with an Arabic audit trail on every number.

- Takes rooms (floor polygons, walls, clear heights, doors and windows) and a finish system per surface, and measures them with formulas from a YAML rules file.
- Each line of the bill shows its inputs, the formula, the same formula with the numbers in it, then net, waste and gross. Anything that cannot be measured becomes an Arabic warning line, not a silent zero.
- Prices the bill from versioned Levant and Gulf price books and renders a print-ready Arabic RTL quotation. The bundled price books are placeholders, so the quotation is stamped as a draft.

Stack: Python (the core needs only the standard library and PyYAML), FastAPI, and a React 19 + TypeScript + Vite + Tailwind 4 web client in `web/`.

## Run

```bash
pip install -r api/requirements.txt
python examples/demo.py          # one room, printed with its Arabic arithmetic
uvicorn api.main:app --reload    # HTTP API, interactive docs at /docs
```

MIT License, see [LICENSE](LICENSE).
