# Auditoria de recuperação e latência

Registro dos achados de medição do pipeline. Existe porque várias decisões do
projeto foram tomadas a partir de números, e um número medido errado é pior que
número nenhum — ele parece justificar a decisão.

---

## E3-01 — `localhost` custava 2 segundos por requisição

**Achado.** Toda chamada feita a `localhost` nesta máquina pagava ~2 s de
timeout do resolver antes de conectar. O custo não aparece em nenhum profile do
código de aplicação: é gasto abaixo do socket, na resolução de nome.

### Causa

`localhost` resolve para dois endereços, nesta ordem:

```
localhost   ->  [('IPv6', '::1'), ('IPv4', '127.0.0.1')]
127.0.0.1   ->  [('IPv4', '127.0.0.1')]
```

O `::1` vem primeiro. E os dois serviços do projeto escutam **apenas em IPv4**:

- o uvicorn sobe com `--host 0.0.0.0`, que é a rota IPv4 — `::` seria a IPv6;
- o Neo4j, na configuração padrão desta instalação, também só liga IPv4.

Então o cliente tenta `::1`, espera o timeout, e só então cai para `127.0.0.1`.
A conexão *funciona* — por isso o defeito passou despercebido. Ela só é lenta.

Medido diretamente contra a porta 7687: `::1` devolve `ConnectionRefusedError`
**após 2031 ms**. A recusa não é imediata, que é o que torna o sintoma confuso.

### Medições

Mediana de 5 execuções, nesta máquina (Windows 11):

| caminho | via `localhost` | via `127.0.0.1` | fator |
|---|---:|---:|---:|
| `GET /api/v1/health` | 2073 ms | 37 ms | **56×** |
| conexão TCP crua na 7687 (Bolt) | 2051 ms | 16 ms | **128×** |

O impacto no Bolt é o mais sério dos dois: atinge **toda conexão nova do pool**
do driver Neo4j, não apenas o salto frontend → API. Ingestão, recuperação e
startup pagavam.

### O que foi corrigido

| arquivo | mudança |
|---|---|
| `spacebio-frontend/src/lib/api.ts` | fallback de `API_BASE_URL` → `http://127.0.0.1:8000` |
| `spacebio-frontend/.env.example` | `VITE_API_URL` → `http://127.0.0.1:8000` |
| `config.py` | default de `NEO4J_URI` → `bolt://127.0.0.1:7687` |
| `.env`, `.env.example` | `NEO4J_URI` → `bolt://127.0.0.1:7687` |
| `.env.example` | seção `CORS_ORIGINS`, antes inexistente |

### Quem estava e quem não estava pagando

Aqui é preciso ser exato, porque a premissa intuitiva está errada.

O `.env` do frontend (não versionado) define
`VITE_API_URL="http://192.168.100.84:8000"` — o IP da LAN, escolhido para
permitir testar de outro dispositivo. Esse endereço é um literal IPv4, então
**resolve direto, sem consultar IPv6**. Medido, mediana de 7 execuções:

| host | resolução | mediana |
|---|---|---:|
| `192.168.100.84` | IPv4 apenas | 31,9 ms |
| `127.0.0.1` | IPv4 apenas | 33,5 ms |
| `localhost` | `::1` e depois IPv4 | 2063,4 ms |

Conclusão: **o frontend, na configuração real, nunca sofreu o atraso.** Os
~2 s atingiam (a) quem rodasse sem `.env`, caindo no antigo fallback
`localhost:8000` do código, e (b) toda conexão ao Neo4j, cujo `NEO4J_URI`
era `bolt://localhost:7687` — esse sim, em uso por todo o pipeline.

O `.env` local **não foi alterado**: 31,9 ms contra 33,5 ms não é ganho, e
trocar pelo loopback quebraria o teste a partir do celular, que foi o motivo de
ele estar assim. O que mudou foi o fallback do código e o `.env.example`, que
governam todo clone novo.

O CORS não precisou mudar: a regex de rede local já aceitava as duas formas em
qualquer porta. Verificado explicitamente:

```
http://localhost:5173       aceita        http://127.0.0.1:5173       aceita
http://localhost:8080       aceita        http://127.0.0.1:8080       aceita
http://192.168.100.84:8080  aceita        https://exemplo-externo.com RECUSA
```

`localhost` continua aceito de propósito: é uma origem que o navegador pode
enviar, e para o CORS `http://localhost:8080` e `http://127.0.0.1:8080` são
origens **distintas**. Quem declarar `CORS_ORIGINS` em produção precisa listar
as duas.

---

## Aviso: medições anteriores à Etapa 2 têm viés

> **As latências registradas antes da Etapa 2 foram medidas por linha de comando
> através de `localhost` e carregam ~2 s de timeout de IPv6.** Não são
> comparáveis aos números atuais, e nenhuma conclusão sobre desempenho tirada
> delas se sustenta sem remedição.

O escopo do aviso é o instrumento, não o produto: o viés estava nas MEDIÇÕES
feitas pelo shell, não no caminho que o usuário percorria pelo navegador — que
ia pelo IP da LAN e já era rápido. Dito de outro modo, o sistema era mais
rápido do que os próprios registros diziam.

O viés é aditivo e quase constante (~2,05 s), então a ordem relativa entre
medições daquele período tende a se manter — mas qualquer afirmação sobre
*proporção* está errada. "O cache é 3× mais rápido que a geração ao vivo" vira
outra coisa quando 2 s dos dois lados são artefato.

Números já remedidos por `127.0.0.1`, válidos:

| operação | latência real |
|---|---:|
| roteador de intenção (`intent.py`) | 16 ms |
| acerto no cache de demonstração | 73 ms |
| geração ao vivo (Gemini, ponta a ponta) | 2635 ms |

### Não confundir com a variabilidade do Gemini

Questão separada e ainda aberta: o free tier do Gemini tem latência
imprevisível. Medido para respostas de comprimento semelhante: **3,2 s, 51 s e
91 s**. Isso não é artefato de rede local — é a fila do provedor, e é o motivo
de a E3-04 (timeout com fallback para evidência) existir.

---

## Como reproduzir

```python
import socket, time, statistics, urllib.request

for h in ("localhost", "127.0.0.1"):
    print(h, socket.getaddrinfo(h, 8000, proto=socket.IPPROTO_TCP))
    ts = []
    for _ in range(5):
        t = time.perf_counter()
        urllib.request.urlopen(f"http://{h}:8000/api/v1/health", timeout=30).read()
        ts.append((time.perf_counter() - t) * 1000)
    print(f"  mediana {statistics.median(ts):.1f} ms")
```

Se as duas medianas vierem próximas, esta máquina não reproduz o problema —
provavelmente os serviços estão ligados também em IPv6, ou o resolver ordena
IPv4 primeiro. O achado é específico de dual-stack com serviço só-IPv4.
