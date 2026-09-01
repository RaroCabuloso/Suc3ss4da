# Suc3ss4da Panel

Painel Node.js para administrar scripts, loader, keys, logs, bans e produtos.

O painel usa apenas as rotas internas `/api/...`. O token da API de arquivos fica no backend, em `config.json`, e nunca deve ser colocado no Roblox ou no frontend.

## Deploy

1. Configure `config.json` com as credenciais do administrador e da API de arquivos.
2. Publique o projeto na Vercel.
3. Use esta URL nos scripts:

```lua
local API_URL = "https://suc3ss4da.vercel.app"
```

O login administrativo dura 30 dias e fica armazenado no dispositivo pelo navegador. Se o segredo de sessao for alterado, sera necessario entrar novamente.

## Scripts Roblox

O exemplo abaixo usa `RequestAsync`, que permite enviar o header da key. Substitua `SUA_KEY` e `API_URL`.

```lua
local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local Player = Players.LocalPlayer
local API_URL = "https://suc3ss4da.vercel.app"

local function request(method, url, body, key)
    local headers = { ["Content-Type"] = "application/json" }
    if key and key ~= "" then
        headers["X-Loader-Key"] = key
    end

    return HttpService:RequestAsync({
        Url = url,
        Method = method,
        Headers = headers,
        Body = body and HttpService:JSONEncode(body) or ""
    })
end

local function RegisterLog(key)
    local data = {
        jogo = game.Name,
        game_id = tostring(game.GameId),
        displaynick = Player.DisplayName,
        nick = Player.Name,
        id = tostring(Player.UserId),
        executor = identifyexecutor and identifyexecutor() or "Unknown",
        place_id = tostring(game.PlaceId),
        job_id = game.JobId,
        version = "1.0.0",
        hwid = "N/A"
    }

    local success, response = pcall(function()
        return request("POST", API_URL .. "/api/log", data, key)
    end)

    if not success or not response.Success then
        return false
    end

    local result = HttpService:JSONDecode(response.Body)
    if result.status == "banido" then
        Player:Kick("\\n[Suc3ss4da]\\nVoce esta banido deste script.")
        return false
    end
    return result.status == "registrado"
end

local function LoadScript(scriptId, key)
    local url = API_URL .. "/api/load/" .. HttpService:UrlEncode(scriptId)
    if key and key ~= "" then
        url = url .. "?key=" .. HttpService:UrlEncode(key)
    end

    local success, content = pcall(function()
        return game:HttpGet(url)
    end)

    if success and content and content ~= "" then
        local func, err = loadstring(content)
        if func then
            task.spawn(func)
            return true
        end
        warn("Erro ao carregar script: " .. tostring(err))
    end
    return false
end

local function CheckBan()
    local success, response = pcall(function()
        return game:HttpGet(API_URL .. "/api/banlist")
    end)

    if not success then
        return false
    end

    local bans = HttpService:JSONDecode(response)
    for _, ban in pairs(bans) do
        if ban.nick == Player.Name or ban.id == tostring(Player.UserId) then
            Player:Kick("\\n[Suc3ss4da]\\nBanimento detectado.")
            return true
        end
    end
    return false
end

local function ValidateKey(key)
    local success, response = pcall(function()
        return game:HttpGet(API_URL .. "/api/key/validate/" .. HttpService:UrlEncode(key))
    end)

    if not success then
        return false, "Nao foi possivel consultar a API."
    end

    local data = HttpService:JSONDecode(response)
    if data.valid then
        if data.key.roblox ~= "" and data.key.roblox ~= Player.Name then
            return false, "Esta chave pertence a outro usuario."
        end
        return true, data.key
    end
    return false, "Chave invalida ou expirada."
end

-- Exemplo de uso:
-- local key = "SUA_KEY"
-- local valid, info = ValidateKey(key)
-- if valid and not CheckBan() then
--     RegisterLog(key)
--     LoadScript("ID_DO_SCRIPT", key)
-- end
```

## Rotas

| Rota | Uso |
| --- | --- |
| `POST /api/log` | Registra uma execucao. Aceita `X-Loader-Key`. |
| `GET /api/load/:id` | Carrega um arquivo loader. |
| `GET /api/raw/:id` | Entrega script raw para requisicoes Roblox. |
| `GET /api/key/validate/:key` | Valida uma key e incrementa os usos. |
| `GET /api/banlist` | Consulta a lista publica de bans. |

## Scripts Grandes (Chunking)

O painel suporta scripts de **qualquer tamanho** através de chunking automático:

- Scripts até **95KB** são salvos como um único arquivo
- Scripts maiores são divididos em chunks de ~95KB cada
- Ao ler, os chunks são combinados automaticamente
- Suportado em: `/api/scripts`, `/api/loader/save`, `/api/raw/:id`

**Exemplo**: Um script de 5MB será dividido em ~53 chunks, cada um enviado separadamente para evitar limites de payload. Você não precisa fazer nada - é totalmente transparente!

### Limite máximo

- Máximo 10.000 chunks por arquivo (~950MB teórico)
- Limite prático é a capacidade da API de armazenamento

### Como usar scripts grandes

1. **No painel admin**: Cole o código (sem limites) e clique em "Salvar"
2. **Via API**: POST para `/api/scripts` com `{"titulo":"...", "codigo":"..."}` (sem limites de tamanho)
3. **No Roblox**: Use a loadstring normalmente - o sistema carrega automaticamente

Nenhuma configuração adicional é necessária. O chunking funciona transparentemente.

## Desenvolvimento local

```bash
npm install
npm start
```

O servidor inicia em `http://localhost:3000`. O arquivo `config.json` e privado e nao deve ser exposto em respostas HTTP.
