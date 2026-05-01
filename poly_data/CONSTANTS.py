# Minimum position size to trigger position merging
# Positions smaller than this will be ignored to save on gas costs
MIN_MERGE_SIZE = 20

# Polymarket Polygon mainnet contracts.
# Keep these in sync with https://docs.polymarket.com/resources/contracts.
CTF_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_CTF_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF_COLLATERAL_ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"
NEG_RISK_CTF_COLLATERAL_ADAPTER = "0xadA2005600Dec949baf300f4C6120000bDB6eAab"

# Legacy adapter kept only for historical reference. Relayer-backed inventory
# actions must use NEG_RISK_CTF_COLLATERAL_ADAPTER.
LEGACY_NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
