-- DECISIONS V1-42. A tenth synthetic issuer: physical metal.
--
-- §4.3 seeds nine (eight in the spec, plus `__NO_DISCLOSURE__` per V1-01). None
-- of them fits a bar of gold in a vault, so Rs 55,869 Cr of it sat in
-- `__UNRESOLVED__` -- 73% of everything still unresolved after V1-41, and by
-- far the largest single line in the warehouse:
--
--     GOLD 995 1KG BAR                         52,449 Cr
--     SILVER                                    3,334 Cr
--     GOLD 99.9 FINENESS - GUJ PHYSICAL SETT       43 Cr
--
-- `__UNRESOLVED__` means "we could not identify this". We can: it is gold.
-- What it does not have is an ISSUER, because it is not a security -- nobody
-- issued it and nobody owes anything on it. That is precisely what a synthetic
-- issuer is for, and it is the same distinction `__CASH__` and `__TREPS__`
-- already draw.
--
-- Being synthetic, it is excluded from overlap and concentration. That is
-- correct rather than a compromise: §8.1's exposure unit is the ISSUER, and two
-- funds holding bullion are not exposed to a common company. The holding is
-- disclosed, counted in the portfolio total, and absent from issuer analysis --
-- which is a true description of what it is.

INSERT OR IGNORE INTO issuer (issuer_id, canonical_name, is_listed, is_synthetic)
VALUES ('__COMMODITY__', 'Physical Commodity', 0, 1);
