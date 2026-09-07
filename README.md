# Monitor de preço — iPhone 17e (Telegram)

Roda todo dia às 09:00 (BRT) no GitHub Actions, varre as fontes, e manda no
Telegram o menor preço do dia com cupom, comparativo com ontem e mínimo histórico.
Custo: R$ 0.

---

## 1. Criar o bot no Telegram (5 min)

1. No Telegram, abra conversa com **@BotFather**
2. Envie `/newbot`, escolha um nome e um username terminado em `bot`
3. Ele devolve o **token** — algo como `8123456789:AAH...`. Guarde.
4. Abra conversa com o seu novo bot e mande qualquer mensagem (`/start`).
   Sem isso o bot não consegue te escrever — o Telegram exige que o usuário
   inicie a conversa.
5. Descubra seu **chat_id**: abra no navegador
   `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates`
   e procure `"chat":{"id":123456789`. Esse número é o `TELEGRAM_CHAT_ID`.

Teste rápido, direto no navegador:

```
https://api.telegram.org/bot<SEU_TOKEN>/sendMessage?chat_id=<SEU_CHAT_ID>&text=teste
```

## 2. Subir o repositório

```bash
git init
git add .
git commit -m "monitor de preco 17e"
git branch -M main
git remote add origin git@github.com:SEU_USUARIO/watch-17e.git
git push -u origin main
```

Repositório **público** = Actions ilimitado. Privado consome a cota mensal
gratuita (essa rotina gasta poucos minutos por mês, cabe folgado).

## 3. Cadastrar os secrets

No GitHub: **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Obrigatório | Valor |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | sim | token do BotFather |
| `TELEGRAM_CHAT_ID` | sim | id obtido no `getUpdates` |
| `ML_ACCESS_TOKEN` | não | token da API do Mercado Livre |

Sem `ML_ACCESS_TOKEN` o script simplesmente pula essa fonte e trabalha só com
os feeds — que são justamente os que trazem os preços com cupom.

## 4. Testar

Aba **Actions → monitor-preco → Run workflow**. A mensagem deve chegar em
menos de 1 minuto.

---

## Ajustes

Tudo em `config.json`:

| Campo | Para que serve |
|---|---|
| `preco_alvo` | Abaixo disso a mensagem marca ✅ |
| `piso_sanidade` / `teto_sanidade` | Descarta valor absurdo (acessório, erro de parse, golpe) |
| `termos_obrigatorios` | Precisa estar no título |
| `termos_proibidos` | Descarta capa, película, seminovo, 17 Pro etc. |
| `feeds` | Fontes RSS |
| `silenciar_se_sem_novidade` | `true` = só avisa quando o preço cai |

Trocar de produto: mude `produto`, `ml_query`, `armazenamento_esperado`,
os termos e o `preco_alvo`. O resto funciona igual.

Mudar o horário: edite o `cron` em `.github/workflows/monitor.yml`.
Ele é **UTC** — subtraia 3 h para chegar no horário de Recife.

---

## Limitações conhecidas

- **Workflow agendado hiberna.** O GitHub desativa `schedule` em repositório
  sem commits por 60 dias. Como o job commita `state.json` todo dia, isso se
  resolve sozinho — mas se a coleta falhar por semanas seguidas, confira.
- **Cron atrasa.** 5 a 30 min de atraso é comportamento normal do Actions,
  não é defeito do script.
- **Feed muda.** Se um feed sair do ar o script loga e segue com os outros;
  nunca quebra a execução inteira. Confira os logs de vez em quando.
- **Promobit e Pelando não têm RSS público** (retornam 404 e 403). Por isso
  ficaram de fora.
- **Scraping de Magalu/Amazon foi deliberadamente evitado** — os dois vedam em
  termos de uso e bloqueiam por IP. As fontes usadas são feeds públicos e a
  API oficial do ML.
- **A API do ML exige token.** A documentação chama `/sites/{site}/search` de
  recurso público, mas todos os exemplos atuais passam `Authorization: Bearer`.
  Registre um app grátis em developers.mercadolivre.com.br. Não confirmei se
  ainda existe acesso sem token — por isso a fonte é opcional.
- **O cupom pode ter expirado.** O monitor lê o que o agregador publicou; ele
  não valida o cupom no carrinho. Sempre confira o valor final antes de pagar.

## Arquivos gerados

- `state.json` — último preço reportado, mínimo histórico, melhor oferta
- `historico.csv` — uma linha por dia, para você plotar a curva depois

