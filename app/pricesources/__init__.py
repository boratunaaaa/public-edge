"""Free historical price recovery for delisted / merged / renamed securities.

A series is never trusted because a provider returned data. Every series passes the identity gate
(`gate.classify`) and only TRUSTED or VALIDATED_FALLBACK series reach the backtest; LOW_CONFIDENCE and
REJECTED series are kept (with the reasons) but never used. Wrong prices are worse than no prices.
"""
ACCEPTED = ("TRUSTED", "VALIDATED_FALLBACK")
