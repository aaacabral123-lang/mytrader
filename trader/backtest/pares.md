# Teste: pares de empresas correlacionadas (comprar a que ficou para trás)

19 pares: KO/PEP, V/MA, HD/LOW, XOM/CVX, JPM/BAC, GS/MS, UPS/FDX, MCD/YUM, WMT/TGT, PG/CL, CAT/DE, T/VZ, UNP/CSX, LMT/NOC, PFE/MRK, COST/WMT, MSFT/AAPL, C/WFC, COP/XOM. 96 versões da regra. **Treino** 2007-01-03 a 2016-12-30, **prova** 2017-01-03 a 2026-11-10. Até 5 operações ao mesmo tempo, mesmo tamanho; custo de 0,05% por compra ou venda.

**Para comparar, só SPY:** treino +6.9% ao ano (pior queda -55.2%), prova +14.7% ao ano (pior queda -33.7%).
**Controle, as mesmas 36 empresas compradas e seguradas em partes iguais (sem a regra dos pares):** treino +11.4% ao ano (pior queda -49.4%), prova +15.6% ao ano (pior queda -33.8%). A regra só agrega valor se render mais do que isso.

## As 10 versões mais robustas (boas no treino e na prova)

| Regra | Treino: ao ano | Treino: pior queda | Prova: ao ano | Prova: pior queda | Prova: acertos | Prova: operações | Prova: média por operação |
| --- | --- | --- | --- | --- | --- | --- | --- |
| z 1.5→0.0 / 20 dias de base / correlação ≥ 0.5 / até 20 dias / stop sem | +16.8% | -35.6% | +18.6% | -44.6% | 62% | 986 | +1.0% |
| z 2.0→0.0 / 20 dias de base / correlação ≥ 0.5 / até 10 dias / stop sem | +14.1% | -37.6% | +15.9% | -40.1% | 57% | 1292 | +0.6% |
| z 1.5→0.5 / 20 dias de base / correlação ≥ 0.5 / até 10 dias / stop sem | +14.1% | -39.4% | +16.9% | -33.4% | 58% | 1700 | +0.5% |
| z 2.0→0.0 / 20 dias de base / correlação ≥ 0.5 / até 20 dias / stop sem | +13.4% | -34.9% | +15.3% | -43.1% | 59% | 908 | +0.9% |
| z 1.5→0.5 / 20 dias de base / correlação ≥ 0.7 / até 20 dias / stop 10% | +14.0% | -39.3% | +14.7% | -38.1% | 59% | 1240 | +0.6% |
| z 1.5→0.5 / 20 dias de base / correlação ≥ 0.5 / até 20 dias / stop 10% | +14.2% | -43.5% | +16.4% | -40.4% | 60% | 1332 | +0.6% |
| z 1.5→0.5 / 20 dias de base / correlação ≥ 0.7 / até 10 dias / stop 10% | +13.2% | -40.7% | +11.5% | -36.0% | 57% | 1488 | +0.4% |
| z 1.5→0.0 / 60 dias de base / correlação ≥ 0.5 / até 20 dias / stop sem | +12.2% | -39.4% | +16.4% | -43.5% | 59% | 691 | +1.2% |
| z 2.0→0.0 / 20 dias de base / correlação ≥ 0.7 / até 20 dias / stop sem | +12.8% | -41.9% | +14.3% | -38.0% | 60% | 790 | +0.9% |
| z 1.5→0.5 / 60 dias de base / correlação ≥ 0.5 / até 20 dias / stop sem | +13.7% | -45.6% | +16.1% | -43.5% | 61% | 775 | +1.1% |

## As 10 melhores só no treino

| Regra | Treino: ao ano | Treino: pior queda | Prova: ao ano | Prova: pior queda | Prova: acertos | Prova: operações | Prova: média por operação |
| --- | --- | --- | --- | --- | --- | --- | --- |
| z 1.5→0.0 / 20 dias de base / correlação ≥ 0.5 / até 20 dias / stop sem | +16.8% | -35.6% | +18.6% | -44.6% | 62% | 986 | +1.0% |
| z 2.5→0.0 / 60 dias de base / correlação ≥ 0.5 / até 20 dias / stop sem | +12.3% | -30.4% | +11.3% | -40.7% | 59% | 442 | +1.3% |
| z 2.0→0.0 / 20 dias de base / correlação ≥ 0.5 / até 20 dias / stop sem | +13.4% | -34.9% | +15.3% | -43.1% | 59% | 908 | +0.9% |
| z 2.0→0.0 / 20 dias de base / correlação ≥ 0.5 / até 10 dias / stop sem | +14.1% | -37.6% | +15.9% | -40.1% | 57% | 1292 | +0.6% |
| z 1.5→0.5 / 20 dias de base / correlação ≥ 0.5 / até 10 dias / stop sem | +14.1% | -39.4% | +16.9% | -33.4% | 58% | 1700 | +0.5% |
| z 1.5→0.5 / 20 dias de base / correlação ≥ 0.7 / até 20 dias / stop 10% | +14.0% | -39.3% | +14.7% | -38.1% | 59% | 1240 | +0.6% |
| z 2.5→0.0 / 20 dias de base / correlação ≥ 0.7 / até 10 dias / stop sem | +8.8% | -26.0% | +4.6% | -38.9% | 57% | 529 | +0.5% |
| z 1.5→0.5 / 20 dias de base / correlação ≥ 0.5 / até 20 dias / stop 10% | +14.2% | -43.5% | +16.4% | -40.4% | 60% | 1332 | +0.6% |
| z 1.5→0.5 / 20 dias de base / correlação ≥ 0.7 / até 10 dias / stop 10% | +13.2% | -40.7% | +11.5% | -36.0% | 57% | 1488 | +0.4% |
| z 1.5→0.0 / 60 dias de base / correlação ≥ 0.5 / até 20 dias / stop sem | +12.2% | -39.4% | +16.4% | -43.5% | 59% | 691 | +1.2% |

_Resultado passado não garante resultado futuro. Não é recomendação de investimento._
