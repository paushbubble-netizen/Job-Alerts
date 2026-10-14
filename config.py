"""Everything you might want to tweak lives here. No other file needs editing."""

# ---- Which job titles count ------------------------------------------------
TITLE_INCLUDE = [
    r"talent partner",
    r"talent acquisition",
    r"\brecruit(er|ers|ing|ment)\b",
    r"head of talent",
    r"talent lead",
    r"talent (manager|specialist|advis[eo]r|business partner)",
    r"people (&|and) talent",
    # r"\bsourcer\b",   # uncomment if you want sourcing roles too (usually junior pay)
]

TITLE_EXCLUDE = [
    r"\b(intern|internship|junior|jr\.?|graduate|apprentice|coordinator|assistant|entry[- ]level|trainee)\b",
    r"\b(university|campus|early career|early talent|student)\b",
    r"talent (agent|agency manager)",
]

# ---- Pay -------------------------------------------------------------------
MIN_SALARY_GBP = 70_000
ALLOW_UNKNOWN_SALARY = True      # most postings don't list pay; True = alert anyway, flagged "salary not listed"

# Rough conversion rates to GBP. Edit if they drift. Only used to compare against the floor.
FX_TO_GBP = {"GBP": 1.0, "USD": 0.75, "EUR": 0.86, "CAD": 0.55, "AUD": 0.49,
             "CHF": 0.90, "SGD": 0.58, "AED": 0.20, "INR": 0.009}

# ---- Location --------------------------------------------------------------
# "Worldwide / anywhere" roles always alert. Plain "Remote" with no country stated
# is ambiguous (often really US-only), so it alerts but is flagged "check eligibility".
ALLOW_UNSPECIFIED_LOCATION = True

# Add regions here if you ever want region-limited remote roles too, e.g. ["uk", "united kingdom", "emea", "europe"]
ACCEPT_REGIONS = []

# ---- Company career boards (the fastest source: roles appear here first) ----
# Token = the bit in the careers-page URL, e.g. boards.greenhouse.io/<token>, jobs.lever.co/<token>,
# jobs.ashbyhq.com/<token>. Wrong/changed tokens are simply skipped (logged in the run output).
# You know the fintech / Web3 / AI hiring landscape far better than I do. Add as many as you like.
GREENHOUSE = ["coinbase", "stripe", "gitlab", "anthropic", "databricks", "chainalysis",
              "consensys", "ripple", "circle", "fireblocks", "okx", "gemini"]
LEVER = ["kraken", "binance", "crypto", "bitgo", "wintermute", "paxos"]
ASHBY = ["ramp", "openai", "notion", "deel", "remote", "sardine", "dualentry", "graswald"]
