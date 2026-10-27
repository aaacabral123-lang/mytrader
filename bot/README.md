# Robô MyTrader (paper)

Um robô que usa as skills `trader-*` para operar sozinho numa conta **paper** (dinheiro falso) da Alpaca.

## O que ele faz

A cada 30 minutos, de segunda a sexta, o GitHub Actions roda `bot/robo.py`. Se a bolsa de NY estiver fechada, ele não faz nada. Com a bolsa aberta:

1. **Confere as ordens anteriores** e atualiza o caixa do robô com o que foi executado.
2. **Uma vez por dia, lê o 13F** do investidor escolhido (`trader-copycat`). O padrão é a Berkshire Hathaway (Buffett), com as 5 maiores posições.
3. **Sincroniza** as posições e os preços da Alpaca.
4. **Uma vez por dia, escreve um relatório** em `trader/relatorios/AAAA-MM-DD.md` com:
   - a carteira: valor, ganho ou perda de cada ação;
   - as notícias e os 8-K das ações que ele tem ou segue (`trader-news`);
   - as declarações novas de compra e venda de deputados dos EUA, com destaque para as suas ações (`trader-pelosi`).
5. **Stop de proteção** (`trader-alerts`): se uma ação cair 15% abaixo do preço médio de compra, ele vende tudo dela e a deixa de fora por 30 dias. Nesse tempo, o dinheiro vai para as outras ações da carteira.
6. **Decide se há oportunidade:**
   - primeira vez: compra a carteira inteira dentro do orçamento (`trader-budget`);
   - depois: só opera se alguma ação se afastou mais de 5 pontos percentuais da meta (`trader-rebalance`). Quando uma ação cai muito, ele compra mais; quando sobe demais, vende um pouco;
   - quando sai um 13F novo, vende o que o investidor vendeu e compra o que ele comprou.
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

## Ajustes

- `trader/config.json`: orçamento (`budget`, padrão US$ 5.000), caixa mínimo (`cash_buffer_pct`, 10%), máximo por ação (`max_position_pct`, 25%), máximo por ordem (`max_order_usd`), ordens por dia (`max_orders_per_day`) e tickers proibidos (`blocked`).
- `trader/robo.json`: liga e desliga o robô (`enabled`), escolhe o investidor (`investor_cik`, `investor_name`; o CIK de qualquer fundo aparece com `python3 .claude/skills/trader-copycat/edgar13f.py find NOME`), define quantas posições copiar (`top`), a faixa de desvio (`band`), o stop (`stop_loss_pct`, 15; use 0 para desligar), os dias fora depois de um stop (`stop_cooldown_days`, 30) e se gera o relatório diário (`daily_report`).

Para **parar o robô**, troque `"enabled": true` por `false` em `trader/robo.json`, ou desative o workflow em Actions.

## Segurança

- O endereço de negociação é fixo em `paper-api.alpaca.markets`, e o robô se recusa a rodar se `config.json` disser `"paper": false`. Para usar dinheiro real seria preciso alterar o código, de propósito.
- Sem opções, margem ou venda a descoberto.
- Não compra nem vende a mesma ação duas vezes no mesmo dia.

Isto não é recomendação de investimento. Copiar um investidor não copia o resultado dele.
