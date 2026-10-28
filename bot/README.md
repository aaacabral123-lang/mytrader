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
