# Hur MCP fungerar, förklarat med Ari

Det här dokumentet går igenom vad en MCP-server och en MCP-klient är, vad som faktiskt skickas mellan dem
och var i koden varje del finns. Alla JSON-meddelanden nedan är riktiga meddelanden fångade från den här
servern, bara förkortade där det står `…`.

Läs gärna med koden öppen bredvid: [`src/ari_mcp/server.py`](../src/ari_mcp/server.py),
[`src/ari_mcp/sandbox.py`](../src/ari_mcp/sandbox.py) och
[`examples/demo_client.py`](../examples/demo_client.py).

---

## 1. Den stora bilden: host, klient och server

MCP (Model Context Protocol) är ett standardiserat sätt för en AI-applikation att använda verktyg och data
som någon annan har byggt. Det finns tre roller:

```
┌──────────────────────────── HOST (t.ex. Claude Desktop, VS Code) ───────────────────────────┐
│                                                                                              │
│   Användaren  ◄──►  Språkmodellen (LLM)                                                      │
│                          │  "jag vill anropa search_documents med query=budget"              │
│                          ▼                                                                   │
│                   ┌─────────────┐        ┌─────────────┐                                     │
│                   │ MCP-klient  │        │ MCP-klient  │   (en klient per server)            │
│                   └──────┬──────┘        └──────┬──────┘                                     │
└──────────────────────────┼──────────────────────┼────────────────────────────────────────────┘
                           │ JSON-RPC över stdio  │ JSON-RPC över HTTP
                           ▼                      ▼
                   ┌───────────────┐      ┌───────────────┐
                   │ MCP-server:   │      │ någon annan   │
                   │ Ari (Python)  │      │ MCP-server    │
                   └───────┬───────┘      └───────────────┘
                           │ läser (bara läser)
                           ▼
                   C:\Store\Dokument
```

- **Hosten** är programmet användaren pratar med. Den äger konversationen och språkmodellen, och den
  bestämmer vilka servrar som är påkopplade och om användaren måste godkänna ett anrop.
- **Klienten** är en del av hosten. Den håller en anslutning till *en* server: startar den, frågar vad den
  kan och skickar vidare anrop. Hosten har en klient per server.
- **Servern** är ett fristående program som erbjuder förmågor. Ari är en server. Den vet ingenting om
  språkmodellen eller konversationen; den får bara förfrågningar och svarar på dem.

Den viktigaste insikten: **modellen pratar aldrig direkt med servern.** Modellen ser bara en lista med
verktygsnamn, beskrivningar och JSON-scheman. När modellen vill använda ett verktyg säger den det till
hosten, hosten låter klienten skicka anropet, och svaret läggs tillbaka i konversationen som text modellen
kan läsa.

## 2. Vad en server erbjuder

En MCP-server kan erbjuda tre sorters saker ("primitives"):

| Primitiv | Vem styr den | Exempel |
| --- | --- | --- |
| **Tools** (verktyg) | Modellen väljer när de anropas | `search_documents(query="budget")` |
| **Resources** (resurser) | Applikationen eller användaren väljer | En fil som bifogas som kontext |
| **Prompts** (mallar) | Användaren väljer, t.ex. som ett snabbkommando | "Sammanfatta dokument X" |

Ari använder bara **tools**, eftersom uppgiften är att låta en *agent* själv bestämma när den ska lista,
läsa och söka. Resurser hade passat om användaren själv skulle välja en fil att bifoga.

## 3. Transporten: hur bytena går fram

MCP-meddelanden är **JSON-RPC 2.0**: små JSON-objekt med `method`, `params` och ett `id` som svaret
upprepar. De kan skickas på två sätt:

- **stdio** (används här). Hosten startar servern som en barnprocess. Klienten skriver en JSON-rad till
  serverns *stdin*, servern svarar med en JSON-rad på *stdout*. Processen lever så länge hosten vill.
- **Streamable HTTP**. Servern körs som en webbtjänst, vilket passar när den ska delas av många användare
  eller köras på en annan maskin.

En följd av stdio som är lätt att missa: **servern får aldrig skriva vanlig text till stdout**, för då
blir protokollet trasigt. Därför går all loggning till *stderr*. Det är därför du ser raden
`Tool 'read_document' failed: …` i terminalen när du kör demoklienten. Den kommer från serverns stderr och
är inte en del av svaret.

## 4. Ett samtal, steg för steg

Säg att användaren frågar Claude: *"När är budgetmötet?"*. Så här ser det ut på tråden mellan klient (C)
och server (S).

### Steg 1. Hosten startar servern

Hosten läser sin konfiguration (se [README](../README.md#connect-it-to-a-host)) och kör ungefär:

```
uv run --directory C:\Users\…\Ari ari-mcp --root C:\Store\Dokument
```

I koden landar det i `main()` i `server.py`, som bygger servern med `create_server(root)` och kör
`.run("stdio")`.

### Steg 2. Handskakning: vem är du och vad kan du?

Det finns två protokollversioner i omlopp, och Ari klarar båda.

**Den klassiska handskakningen (protokoll 2025-11-25)**, som de flesta hosts använder idag. Klienten öppnar
en session med `initialize`:

```json
C→S {"jsonrpc":"2.0","id":1,"method":"initialize",
     "params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"mcp","version":"0.1.0"}}}

S→C {"jsonrpc":"2.0","id":1,"result":{
       "protocolVersion":"2025-11-25",
       "capabilities":{"tools":{"listChanged":false}, …},
       "instructions":"Read-only access to a folder of public documents …",
       "serverInfo":{"name":"ari-documents","title":"Ari public documents","version":"0.1.0"}}}

C→S {"jsonrpc":"2.0","method":"notifications/initialized"}
```

Lägg märke till att det sista meddelandet saknar `id`. Det är en *notifiering*, alltså ett meddelande som
inte väntar på svar. `instructions` är texten från `MCPServer(instructions=…)` i `server.py`; hosten kan ge
den till modellen som en ledtråd om hur servern ska användas.

**Den nyare, tillståndslösa varianten (protokoll 2026-07-28)**, som SDK:n väljer när båda sidor kan den.
Där finns ingen session. Klienten frågar `server/discover`, och varje senare förfrågan bär själv med sig
protokollversion och klientinfo i `_meta`:

```json
C→S {"jsonrpc":"2.0","id":1,"method":"server/discover",
     "params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28", …}}}

S→C {"jsonrpc":"2.0","id":1,"result":{"supportedVersions":["2026-07-28"],
       "capabilities":{"tools":{"listChanged":true}, …}, "instructions":"…", …}}
```

Testet `tests/test_stdio.py` kör hela vägen med båda varianterna mot en riktig serverprocess.

### Steg 3. Upptäckt: vilka verktyg finns?

```json
C→S {"jsonrpc":"2.0","id":2,"method":"tools/list"}

S→C {"jsonrpc":"2.0","id":2,"result":{"tools":[
      {"name":"search_documents",
       "description":"Search all documents for a word or phrase (case-insensitive) and return matching lines.\n\nArgs: …",
       "annotations":{"readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":false},
       "inputSchema":{"type":"object","properties":{
           "query":{"type":"string","minLength":1,"title":"Query"},
           "max_results":{"type":"integer","default":20,"minimum":1,"maximum":200,"title":"Max Results"}},
           "required":["query"]},
       "outputSchema":{ … SearchResults … }},
      … list_documents, read_document …]}}
```

Här syns det SDK:n gör åt dig. Ingenting av detta är handskrivet JSON. Det kommer från Python-koden:

| I koden (`server.py`) | Blir i protokollet |
| --- | --- |
| `@mcp.tool(...)` på `def search_documents(...)` | ett verktyg med `"name":"search_documents"` |
| Docstringen | `description`, som modellen läser för att förstå när verktyget passar |
| Typen `query: str = Field(min_length=1)` | `inputSchema` med `"type":"string","minLength":1` |
| Returtypen `-> SearchResults` (en Pydantic-modell) | `outputSchema`, och svaret får `structuredContent` |
| `annotations=READ_ONLY` | `annotations` med `readOnlyHint: true` |

Hosten gör om listan till verktygsdefinitioner för modellen. **Beskrivningarna är alltså en del av
gränssnittet**: en bra docstring gör att modellen väljer rätt verktyg med rätt argument.

### Steg 4. Modellen bestämmer sig för ett anrop

Modellen ser frågan *"När är budgetmötet?"* och verktyget `search_documents`, och svarar hosten med något
i stil med "anropa `search_documents` med `{"query":"budget"}`". Hosten kan fråga användaren om lov först.
Sedan skickar klienten:

```json
C→S {"jsonrpc":"2.0","id":3,"method":"tools/call",
     "params":{"name":"search_documents","arguments":{"query":"budget"}}}
```

### Steg 5. Servern kör verktyget

SDK:n validerar `arguments` mot `inputSchema` (ett tomt `query` stoppas här, innan din kod körs) och anropar
sedan Python-funktionen `search_documents`. Den går igenom dokumenten med `iter_documents`, som bara
släpper igenom filer som `DocumentRoot.is_inside` godkänner, och returnerar ett `SearchResults`-objekt:

```json
S→C {"jsonrpc":"2.0","id":3,"result":{
       "content":[{"type":"text","text":"{\n  \"query\": \"budget\",\n  \"hits\": [ … ] … }"}],
       "structuredContent":{"query":"budget","hits":[
           {"path":"meeting-notes-2026-10.md","line":10,
            "snippet":"The budget meeting is on Friday 14 November."}],
         "truncated":false},
       "isError":false}}
```

Svaret kommer i två former samtidigt. `content` är text som alla klienter klarar, och `structuredContent`
är samma data som riktig JSON som följer `outputSchema`, så att program kan använda den direkt.

### Steg 6. Hosten låter modellen svara

Hosten lägger tillbaka resultatet i konversationen, och modellen svarar användaren: *"Budgetmötet är
fredag 14 november, enligt mötesanteckningarna från oktober."* Behöver modellen mer kan den anropa
`read_document` på samma fil, och då upprepas steg 4 till 6.

## 5. När något är fel: verktygsfel och protokollfel

Om agenten försöker ta sig ut ur mappen:

```json
C→S {"jsonrpc":"2.0","id":4,"method":"tools/call",
     "params":{"name":"read_document","arguments":{"path":"../secret.txt"}}}

S→C {"jsonrpc":"2.0","id":4,"result":{
       "content":[{"type":"text",
         "text":"Error executing tool read_document: Access denied: Path is outside the document folder: '../secret.txt'"}],
       "isError":true}}
```

Observera att det är ett vanligt `result` med `"isError":true`, inte ett JSON-RPC-`error`. Det är med
flit. Ett **verktygsfel** är något modellen ska få se och kunna reagera på ("aha, jag ska använda en sökväg
från `list_documents`"). Ett **protokollfel** (JSON-RPC `error`) betyder att själva förfrågan var trasig,
till exempel en okänd metod.

I koden skapas verktygsfelet genom att `safe_path()` fångar `PathOutsideRootError` och kastar `ToolError`.
SDK:n gör om det till `isError: true`. Andra, oväntade undantag skulle bara ge modellen ett generiskt
felmeddelande, så att interna detaljer inte läcker ut.

## 6. Säkerheten i Ari, och varför den ser ut som den gör

Servern körs med användarens rättigheter, så den *kan* tekniskt sett läsa hela datorn. Spärren i
`sandbox.py` är det enda som håller den inne i `C:\Store\Dokument`.

`DocumentRoot.resolve()` gör tre saker i ordning:

1. **Avvisar absoluta sökvägar direkt**, i både Windows- och Unix-form: `C:\…`, `D:fil`, `\\server\share`,
   `\fil` och `/etc/…`. Det kontrolleras med `PureWindowsPath` och `PurePosixPath` så att regeln är
   densamma oavsett vilket operativsystem servern kör på.
2. **Löser upp sökvägen helt** med `Path.resolve()`. Då försvinner `..`, och symlänkar och Windows-junctions
   följs till sitt verkliga mål.
3. **Kontrollerar att resultatet fortfarande ligger under roten** med `is_relative_to()`.

Varför räcker det inte att leta efter `..` i strängen? För att det finns för många vägar runt en sådan
kontroll: `policies/../../x`, blandade snedstreck, och framför allt symlänkar, där sökvägen ser oskyldig ut
men pekar någon annanstans. Att jämföra det *upplösta* resultatet med den *upplösta* roten fångar alla
varianter med en enda regel.

Samma regel gäller för listning och sökning (`is_inside()`), så en symlänk i mappen som pekar på en hemlig
fil syns inte i `list_documents` och läcker inte via `search_documents` heller. Testerna i
`tests/test_sandbox.py` och `tests/test_server.py` prövar just de här fallen.

En sak servern inte kan skydda mot: **innehållet i dokumenten är opålitlig indata för modellen.** Ett
offentligt dokument kan innehålla text som ser ut som instruktioner ("ignorera tidigare instruktioner
och …"). Det är hostens och modellens sak att behandla verktygsresultat som data. Att Ari bara kan läsa
gör att skadan blir begränsad även om det skulle hända.

## 7. Prova själv

| Vad | Kommando | Vad du ser |
| --- | --- | --- |
| Testerna | `uv run pytest -v` | 32 tester för spärr, verktyg och stdio |
| Egen klient | `uv run python examples/demo_client.py sample-docs --query budget` | Upptäckt, tre anrop och ett nekat |
| MCP Inspector | `npx @modelcontextprotocol/inspector uv run ari-mcp --root sample-docs` | Webbgränssnitt där du anropar verktygen för hand |
| Claude Desktop | Se README | Claude använder verktygen i en vanlig chatt |

[`examples/demo_client.py`](../examples/demo_client.py) är värd att läsa rad för rad. Den gör i liten skala
exakt det hosten gör: startar servern (`StdioServerParameters`), ansluter (`async with Client(...)`), frågar
efter verktyg (`list_tools`) och anropar dem (`call_tool`). Det enda som saknas är språkmodellen som
bestämmer *vilka* anrop som ska göras.

## 8. Ordlista

| Ord | Betydelse |
| --- | --- |
| **Host** | Programmet användaren pratar med, som innehåller modellen och en eller flera klienter. |
| **Klient** | Den del av hosten som pratar med en server. |
| **Server** | Programmet som erbjuder verktyg, resurser eller promptmallar. Här: Ari. |
| **Tool** | En funktion modellen kan välja att anropa, med ett JSON-schema för argumenten. |
| **JSON-RPC** | Formatet på meddelandena: `method`, `params`, `id`, och `result` eller `error` i svaret. |
| **stdio** | Transport där klienten startar servern och pratar via stdin/stdout. |
| **Capabilities** | Det servern och klienten talar om att de stöder, i handskakningen. |
| **`structuredContent`** | Verktygssvaret som maskinläsbar JSON, enligt verktygets `outputSchema`. |
| **`isError`** | Markerar ett verktygsfel som modellen ska få se, till skillnad från ett protokollfel. |
| **Annotations** | Tips om verktyget, t.ex. `readOnlyHint`, som hosten kan visa för användaren. |
