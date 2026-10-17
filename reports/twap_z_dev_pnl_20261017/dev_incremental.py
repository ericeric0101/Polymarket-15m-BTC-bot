"""Does the TWAP z-model add information beyond the market price? (DEV, exploratory)
Logistic regression y_up ~ a + b1*logit(mid_up) + b2*z/k, on BTC checkpoints with a causal book,
fit on Train, scored on Valid. If b2 > 0 and Valid log-loss beats 'market only', the model has
incremental information -> a tradable edge can exist."""
import sys
from math import erf, sqrt
from pathlib import Path
import numpy as np, pandas as pd
O = Path(__file__).resolve().parent
sys.path.insert(0, str(O))
import dev_pnl as D  # noqa: E402
import dev_lead_model as L  # noqa: E402

g = L.build()
g = g[g.z_lead.notna()].copy()
books = D.load("data/polymarket_book_1s", ["ts_sec", "market_slug", "outcome", "best_bid", "best_ask",
                                           "last_update_received_ms", "sample_ms"])
if "sample_ms" not in books:
    books["sample_ms"] = np.nan
books = books[books.market_slug.str.startswith("btc-updown-")]
bx = D.BookIndex(books)
mids = []
for r in g.itertuples():
    up = bx.asof(r.slug, "Up", r.t)
    mids.append((up.best_bid + up.best_ask) / 2 if D._valid(up) else np.nan)
g["mid"] = mids
g = g[g.mid.between(0.02, 0.98)].copy()          # informative region (not already decided)
g["y"] = g.up.astype(float)
g["lm"] = np.log(g.mid / (1 - g.mid))
for m in ("base", "lead"):
    g[f"zz_{m}"] = g[f"z_{m}"].clip(-8, 8) / 1.5


def fit(X, y, iters=50):
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w)); W = p * (1 - p) + 1e-9
        H = X.T @ (X * W[:, None]) + 1e-6 * np.eye(X.shape[1]); w += np.linalg.solve(H, X.T @ (y - p))
    p = 1 / (1 + np.exp(-X @ w)); W = p * (1 - p)
    se = np.sqrt(np.diag(np.linalg.inv(X.T @ (X * W[:, None]) + 1e-9 * np.eye(X.shape[1]))))
    return w, se


def ll(X, w, y):
    p = np.clip(1 / (1 + np.exp(-X @ w)), 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


tr, va = g[g.start < L.SPLIT], g[g.start >= L.SPLIT]
print(f"rows with live-ish book (0.02<mid<0.98): train={len(tr)} valid={len(va)}  markets valid={va.slug.nunique()}")
one = lambda d, cols: np.column_stack([np.ones(len(d))] + [d[c].to_numpy() for c in cols])
for name, cols in (("market only", ["lm"]), ("model base only", ["zz_base"]), ("model lead only", ["zz_lead"]),
                   ("market + base", ["lm", "zz_base"]), ("market + lead", ["lm", "zz_lead"])):
    w, se = fit(one(tr, cols), tr.y.to_numpy())
    print(f"{name:16} coef={np.round(w[1:], 3)} t={np.round(w[1:] / se[1:], 1)} | valid logloss={ll(one(va, cols), w, va.y.to_numpy()):.4f}")
# by time band for market+lead
for lo, hi in ((5, 25), (30, 55)):
    sub = tr[(tr.h >= lo) & (tr.h <= hi)]
    w, se = fit(one(sub, ["lm", "zz_lead"]), sub.y.to_numpy())
    print(f"  T-{lo}..T-{hi}: market+lead coef={np.round(w[1:], 3)} t={np.round(w[1:] / se[1:], 1)} n={len(sub)}")
