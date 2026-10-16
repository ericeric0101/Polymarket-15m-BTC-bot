# Aggressive SELL Adapter Capability

- Verdict: **NOT_PROVEN_MARKET_PATH_IS_FOK**
- Requested strategy TIF: `IOC`
- Limit IOC conversion: `FAK`
- Generic market path venue type: `FOK`
- Partial fill supported on market path: `false`
- Insufficient visible/executable depth can reject the entire order: `true`
- Limit IOC uses the installed adapter TIF converter: `true`
- Price-bounded limit IOC avoids the FOK full-depth requirement: `true`

## Conclusion

The installed adapter does **not** prove safe partial aggressive SELL execution. Its generic market path is venue-effective FOK, so insufficient depth may reject the whole order. A limit IOC request converts to FAK, but this static capability check is not a live venue fill proof. Live activation remains blocked pending adapter-level integration evidence for cancel, partial fill, residual inventory, and retry handling.

## Evidence

market path forces PolyOrderType.FOK; limit path delegates IOC through the FAK converter
