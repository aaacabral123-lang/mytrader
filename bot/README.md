# Robô MyTrader (paper)

Um robô que usa as skills `trader-*` para operar sozinho numa conta **paper** (dinheiro falso) da Alpaca.

## O que ele faz

A cada 30 minutos, de segunda a sexta, o GitHub Actions roda `bot/robo.py`. Se a bolsa de NY estiver fechada, ele não faz nada. Com a bolsa aberta:

1. **Confere as ordens anteriores** e atualiza o caixa do robô com o que foi executado.
2. **Uma vez por dia, escolhe as ações** pelos números das empresas:
   - `trader-analyst` lê na SEC os balanços das 100 empresas da lista `universe` (receita, lucro, margens, crescimento);
   - `trader-screener` aplica as regras de `trader/rules.json`: por padrão, receita crescendo pelo menos 10% em 12 meses, margem operacional de pelo menos 25%, lucro positivo e P/L (preço/lucro) até 35;
   - as 8 melhores notas entram na carteira, com pesos iguais. Uma ação escolhida fica enquanto continuar passando nas regras; se deixar de passar, é vendida e a próxima da lista entra.
3. **Sincroniza** as posições e os preços da Alpaca.
4. **Uma vez por dia, escreve um relatório** em `trader/relatorios/AAAA-MM-DD.md` com:
   - a carteira: valor, ganho ou perda de cada ação;
   - por que essas ações: as que passaram no screener, com crescimento, margem, P/L e nota;
   - as notícias e os 8-K das ações que ele tem ou segue (`trader-news`);
   - as declarações novas de compra e venda de deputados dos EUA, com destaque para as suas ações (`trader-pelosi`).
5. **Stop de proteção** (`trader-alerts`): se uma ação cair 15% abaixo do preço médio de compra, ele vende tudo dela e a deixa de fora por 30 dias. Nesse tempo, o dinheiro vai para as outras ações da carteira.
5b. **Realizar lucro** (`trader-alerts`): se uma ação subir 10% acima do preço médio de compra, ele vende tudo dela e guarda esse dinheiro. Recompra quando ela cair 5% abaixo do preço de venda, ou depois de 10 dias se não cair.
6. **Decide se há oportunidade:**
   - primeira vez: compra a carteira inteira dentro do orçamento (`trader-budget`);
   - depois: só opera se alguma ação se afastou mais de 2 pontos percentuais da meta (`trader-rebalance`). Quando uma ação cai muito, ele compra mais; quando sobe demais, vende um pouco;
   - quando o screener troca uma ação, vende a que saiu e compra a que entrou.
7. **Passa cada ordem pelo controle de risco** (`trader-risk`) e só envia as aprovadas (PASS).
8. **Registra tudo** em `trader/log/robo-AAAA-MM.md` e salva o estado no repositório.

## Longo prazo no índice (estratégia atual)

Com `"strategy": "indice"` em `trader/robo.json`, os 80% de longo prazo ficam inteiros no **VOO**, um ETF do índice S&P 500 (as 500 maiores empresas dos EUA), sem stop e sem venda para realizar lucro (`stop_loss_pct` e `take_profit_pct` em 0). O VOO segue o mesmo índice que o SPY, mas é outro ETF, para não se misturar com o curto prazo, que opera o SPY. No teste de 20 anos, segurar o índice foi o melhor resultado (+10,8% ao ano). Nas duas primeiras semanas reais, a escolha de ações pelo screener concentrou a carteira em tecnologia, e o stop vendeu Nvidia e Micron no fundo. Para voltar ao screener, use `"strategy": "screener"`.

## Filtro de mercado (a favor na alta, contra na queda)

**Desligado** (`"enabled": false`): no teste no passado (`trader/backtest/resultado.md`), trocar para o SH nas quedas rendeu +0,5% ao ano, contra +10,8% de só segurar o SPY.

O longo prazo acompanha a tendência do mercado, medida pelo SPY e pela média dos últimos 200 dias (`market_filter` em `trader/robo.json`):

- **Mercado em alta** (SPY acima da média): fica nas ações escolhidas pelo screener.
- **Mercado em queda** (SPY fecha 2% abaixo da média): vende as ações e coloca a parte de ações no **SH**, ETF que sobe quando o S&P 500 cai.
- **Volta para as ações** quando o SPY sobe de novo acima da média.

A margem de 2% evita trocas a cada pequena oscilação. O robô não prevê a queda: ele reage quando a tendência já virou. Por isso protege nas quedas longas, mas pode trocar tarde ou à toa quando o mercado só balança perto da média.

## Pares (30% do dinheiro)

Ideia de um amigo do usuário, testada em `trader/backtest/pares.md`: em pares de empresas que andam juntas (Coca-Cola e Pepsi, Visa e Mastercard, Exxon e Chevron, 19 pares no total), quando uma fica para trás da outra (a distância entre as duas, medida em 20 dias, passa de 1,5 desvio) e elas vêm andando juntas (correlação de 120 dias acima de 0,5), o robô compra a que ficou para trás. Vende quando a distância volta ao normal, ou depois de 20 dias. São até 5 operações ao mesmo tempo, a partir das 15h de Nova York, com caixa próprio e controle de risco próprio (`trader/pares/`).

No teste de 2007 a 2026, a regra rendeu +16,8% ao ano no treino e +18,6% na prova, contra +11,4% e +15,6% de só segurar as mesmas empresas e +6,9% e +14,7% do SPY. Fica sempre investida em ações, então nas crises cai como o mercado (pior queda de -45% na prova).

Divisão atual do robô: 50% no índice (VOO), 30% nos pares e 20% no curto prazo.

## Curto prazo (20% do dinheiro)

Uma parte do robô (`curto` em `trader/robo.json`, 20% dos US$ 5.000 = US$ 1.000) opera **ETFs** no curto prazo, com caixa próprio. Os ETFs são SPY, QQQ, IWM, DIA, os de setor XL* e o SMH. Como o longo prazo só tem ações, as duas partes nunca compram a mesma coisa.

- **Compra:** a partir das 15h de Nova York (16h ou 17h em Brasília, dependendo do horário de verão), quando um ETF está acima da média de 200 dias (tendência de alta) e teve uma queda forte de 2 a 3 dias (RSI de 2 dias abaixo de 10). No máximo 4 ETFs ao mesmo tempo, cada um com 1/4 do dinheiro do curto prazo.
- **Venda:** quando o preço volta para cima da média de 5 dias (o repique), ou cai 8% abaixo da compra (stop), ou depois de 10 dias.
- **Ganhar na queda (ETFs inversos):** SH, PSQ, DOG e RWM sobem quando SPY, QQQ, DIA e IWM caem. Quando um desses índices está em tendência de queda (abaixo da média de 200 dias) e teve uma alta forte de 2 a 3 dias (RSI de 2 dias acima de 90), o robô compra o inverso dele. Vende quando o índice volta a cair abaixo da média de 5 dias, com o mesmo stop de 8% ou depois de 10 dias. Como ele só compra o ETF inverso, a perda máxima é o valor colocado: não há venda a descoberto nem margem.
- **Risco:** cada ordem passa pelo `trader-risk` com regras próprias (`trader/curto/`).

**Só curto prazo:** com `"long_term": false` em `trader/robo.json`, o robô vende as ações do longo prazo, passa todo o dinheiro para o curto prazo e deixa de rodar o screener. Para voltar, use `true`: o longo prazo começa do zero com o caixa que tiver.

O painel e o relatório mostram as duas partes separadas. Para desligar o curto prazo, use `"enabled": false` dentro de `curto`. Os ETFs que ele tiver continuam na conta e podem ser vendidos à mão.

## Time de robôs

Cada skill tem um robô com seu próprio ritmo (`bot/equipe.py`). O time roda 24 horas: a cada 30 minutos, um turno do GitHub chama todos os robôs que estiverem na hora.

| Robô | Skill | Quando | O que faz |
| --- | --- | --- | --- |
| Gestor | budget, rebalance, risk, broker | a cada 30 min com a bolsa aberta | compra e vende (longo e curto prazo) |
| Notícias | trader-news | a cada 30 min, 24 h | comunicado grave da própria empresa à SEC (8-K de falência, ataque hacker, dívida antecipada, baixa contábil, risco de sair da bolsa, balanço refeito): avisa e bloqueia novas compras dela por 5 dias. Só entram notícias de fontes confiáveis: comunicados à SEC e Reuters, AP, Bloomberg, WSJ, Financial Times, CNBC, MarketWatch, Barron's, New York Times e Yahoo Finance (lista em `noticias_fontes` no `trader/robo.json`). Manchetes com palavras como \"processo\" ou \"hack\" só aparecem no painel |
| Políticos | trader-pelosi | a cada 2 h, 24 h | avisa quando um deputado negociar ações que o robô tem ou segue |
| Vigia de preços | trader-alerts | a cada 30 min com a bolsa aberta | avisa quando uma ação sua sobe ou cai 5% no dia |
| Analista | trader-analyst | 1x por dia, 7h de Nova York | balanço novo de uma ação sua: avisa e pede ao gestor que refaça a escolha das ações |
| Copiador | trader-copycat | 1x por dia, 8h de Nova York | avisa quando a Berkshire (Buffett) entrega um 13F novo |
| Contador | trader-tax | 1x por dia, 16h30 de Nova York | ganhos e perdas por lote (`trader/time/impostos.json`) |

Os robôs passam sinais ao gestor em `trader/time/sinais.json` e mostram o status no painel ("Time de robôs").

## Configuração (uma vez só)

1. Na Alpaca, entre na conta **Paper**. Se já tiver posições lá, use *Reset* para começar do zero, porque o robô considera tudo o que está na conta. Gere as chaves de API do paper.
2. No GitHub, abra o repositório → **Settings → Secrets and variables → Actions → New repository secret** e crie três secrets:
   - `ALPACA_API_KEY`: a key ID do paper
   - `ALPACA_SECRET_KEY`: a secret key do paper
   - `SEC_USER_AGENT`: seu nome e e-mail, por exemplo `Andre Cabral seu@email.com` (a SEC exige isso)
3. Faça o merge deste PR no `main`. Os agendamentos do GitHub só rodam a partir do `main`.
4. Para testar na hora: **Actions → Robô MyTrader (paper) → Run workflow**. Marque "Simular sem enviar ordens" para ver o que ele faria sem enviar nada.

**Nunca cole as chaves no chat nem em arquivos do repositório.** Elas ficam só nos Secrets.

## No celular

**Relatório:** instale o app do GitHub, abra o repositório `mytrader` e vá na pasta `trader/relatorios`. Cada dia de pregão tem um arquivo com a data.

**Painel (saldo, carteira, gráfico e operações):** abra https://aaacabral123-lang.github.io/mytrader/painel/ no celular e use "Adicionar à Tela de Início" (iPhone: botão Compartilhar no Safari; Android: menu ⋮ no Chrome). Ele vira um ícone de app. Os dados são atualizados a cada rodada do robô.

**Notificações** (com o app gratuito [ntfy](https://ntfy.sh), sem cadastro):
1. Instale o **ntfy** no celular (App Store ou Google Play).
2. No app, toque em **+** e assine um tópico com um nome secreto, por exemplo `mytrader-` seguido de letras e números aleatórios. Quem souber o nome consegue ler as mensagens, então não use nada fácil de adivinhar.
3. No GitHub, crie o secret `NTFY_TOPIC` com esse mesmo nome.

Você recebe um aviso a cada compra ou venda executada, quando um stop é acionado, quando o relatório do dia fica pronto (tocar abre o relatório) e se o robô der erro. Sem o secret `NTFY_TOPIC`, o robô simplesmente não envia notificações.

## Ajustes

- `trader/rules.json`: as regras do screener. Operadores: `>`, `>=`, `<`, `<=`, `between`, `in`, `not in`. Métricas: `revenue_growth_yoy`, `eps_growth_yoy`, `gross_margin`, `operating_margin`, `net_margin`, `eps_ttm`, `pe`, `earnings_yield`. A seção `rank` define a nota.
- `trader/config.json`: orçamento (`budget`, padrão US$ 5.000), caixa mínimo (`cash_buffer_pct`, 10%), máximo por ação (`max_position_pct`, 25%), máximo por ordem (`max_order_usd`), ordens por dia (`max_orders_per_day`) e tickers proibidos (`blocked`).
- `trader/robo.json`: liga e desliga o robô (`enabled`), escolhe a estratégia (`strategy`: `screener` ou `copycat`, para voltar a copiar um investidor pelo 13F com `investor_cik` e `investor_name`), a lista de empresas candidatas (`universe`), de quantos em quantos dias refazer a escolha (`screen_every_days`, 1), quantas ações ter (`top`, 8), a faixa de desvio (`band`), o stop (`stop_loss_pct`, 15; use 0 para desligar), os dias fora depois de um stop (`stop_cooldown_days`, 30), o lucro para vender (`take_profit_pct`, 10; 0 desliga), a queda para recomprar (`take_profit_reentry_dip_pct`, 5), o prazo máximo de espera (`take_profit_max_days`, 10) e se gera o relatório diário (`daily_report`).

Para **parar o robô**, troque `"enabled": true` por `false` em `trader/robo.json`, ou desative o workflow em Actions.

## Segurança

- O endereço de negociação é fixo em `paper-api.alpaca.markets`, e o robô se recusa a rodar se `config.json` disser `"paper": false`. Para usar dinheiro real seria preciso alterar o código, de propósito.
- Sem opções, margem ou venda a descoberto.
- Não compra nem vende a mesma ação duas vezes no mesmo dia.

Isto não é recomendação de investimento. Copiar um investidor não copia o resultado dele.
