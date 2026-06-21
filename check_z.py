import os
import magicformula as mf

# Pull annual statements for XXX
prof = mf.fmp_get("/profile/HPQ")
inc = mf.fmp_get("/income-statement/HPQ", {"period": "annual", "limit": 2})
bal = mf.fmp_get("/balance-sheet-statement/HPQ", {"period": "annual", "limit": 2})

# Current year (index 0) and prior year (index 1)
print("Current Z:", mf._compute_z_score(prof[0], [inc[0]], [bal[0]]))
print("Prior year Z:", mf._compute_z_score(prof[0], [inc[1]], [bal[1]]))
