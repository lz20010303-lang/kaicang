import os

# Tests must not hit the network from the startup region self-check.
os.environ.setdefault("REGION_PROBE", "no")
