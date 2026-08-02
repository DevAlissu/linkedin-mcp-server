# 🇧🇷 linkedin-mcp-server

**Servidor MCP de LinkedIn que funciona de verdade em conta brasileira — leitura, mensageria e edição de perfil.**

<p align="left">
  <a href="https://github.com/DevAlissu/linkedin-mcp-server/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-3fb950?labelColor=32383f" alt="License"></a>
  <a href="https://github.com/DevAlissu/linkedin-mcp-server/actions/workflows/ci.yml"><img src="https://github.com/DevAlissu/linkedin-mcp-server/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <img src="https://img.shields.io/badge/MCP-Model_Context_Protocol-d97757" alt="MCP">
  <img src="https://img.shields.io/badge/Python-3.12+-3776ab?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/i18n-PT--BR_%2B_EN-ffdc53" alt="i18n">
  <img src="https://img.shields.io/badge/edi%C3%A7%C3%A3o_de_perfil-12_tools-8957e5" alt="Edicao de perfil">
</p>

> **Aviso:** projeto independente, sem afiliação, autorização, endosso ou patrocínio da LinkedIn Corporation ou da Microsoft. "LinkedIn" é marca registrada da LinkedIn Corporation, usada aqui apenas para identificar o serviço com o qual este software interopera.

Um servidor MCP que dá a assistentes de IA (Claude, entre outros) acesso ao LinkedIn através da **sua própria sessão de navegador** — sem API oficial, sem token de parceiro. Lê perfis e empresas, busca vagas, gerencia mensagens e **edita o seu próprio perfil**.

---

## 💡 Por que esse fork?

O projeto original é excelente, mas foi escrito assumindo interface em **inglês**. Em conta brasileira, boa parte das ações de escrita falhava — às vezes em silêncio:

| Problema | Por que acontecia | Aqui |
|---|---|---|
| Modal de edição não abria | Deep-link `/overlay/edit/...` não monta o dialog "a frio"; o painel Premium interceptava o clique | ✅ Abre pelo controle da própria página, com clique escalado (normal → force → JS) |
| Campos não eram encontrados | O código procurava `"Headline"`; a interface mostra `"Título"`/`"Cargo"` | ✅ Camada i18n com aliases EN/PT-BR |
| Headline não preenchia | É uma `div contenteditable` sem rótulo associado | ✅ Alvo estrutural + preenchimento direto |
| Botão de salvar não clicava | Procurava `"Save"`; o botão é `"Salvar"` | ✅ Casamento por nome acessível, com tabela por idioma |
| Seção "Sobre" não editava | O LinkedIn usa a rota `/edit/forms/summary`, não `/overlay/edit/about` | ✅ Rota correta, com editor flexível |
| Seções longas liam incompletas | A paginação procurava `"Show more"`; aparece `"Ver mais"` | ✅ Paginação localizada |

Adicionar um novo idioma agora é editar **um arquivo** (`scraping/i18n.py`), não caçar strings pelo código todo.

## 🚀 Início rápido

Requer [uv](https://docs.astral.sh/uv/) e Python 3.12+.

```bash
# 1. Clonar
git clone https://github.com/DevAlissu/linkedin-mcp-server.git
cd linkedin-mcp-server

# 2. Instalar dependências
uv sync

# 3. Instalar o navegador usado na automação
uv run patchright install chromium

# 4. Fazer login (abre uma janela para você entrar na sua conta)
uv run -m linkedin_mcp_server --login --no-headless
```

Registrar no Claude Code:

```bash
claude mcp add linkedin -- uv run --directory /caminho/para/linkedin-mcp-server mcp-server-linkedin
```

Ou em qualquer cliente MCP (`claude_desktop_config.json` e afins):

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "uv",
      "args": ["run", "--directory", "/caminho/para/linkedin-mcp-server", "mcp-server-linkedin"]
    }
  }
}
```

A sessão fica salva em `~/.linkedin-mcp/profile/` e é reutilizada — você faz login uma vez só.

> **Importante:** o servidor é um processo de vida longa. Se você alterar o código, reinicie a conexão MCP (no Claude Code: `/mcp` → reconnect) para o código novo valer.

## 🎯 Ferramentas

### Leitura

| Tool | O que faz |
|------|-----------|
| `get_person_profile` | Perfil de uma pessoa, com seleção de seções (experiência, formação, competências, projetos, certificações, contato, posts…) |
| `get_my_profile` | Seu próprio perfil |
| `get_sidebar_profiles` | Perfis sugeridos na barra lateral de um perfil |
| `get_company_profile` | Dados de uma empresa, com seleção de seções |
| `get_company_posts` | Posts recentes de uma empresa |
| `get_company_employees` | Funcionários de uma empresa, com filtro opcional |
| `get_job_details` | Detalhes de uma vaga |
| `get_feed` | Posts recentes do seu feed |

### Busca

| Tool | O que faz |
|------|-----------|
| `search_people` | Pessoas por palavra-chave, local, grau de conexão (1º/2º/3º) e empresa atual |
| `search_companies` | Empresas por palavra-chave |
| `search_jobs` | Vagas por palavra-chave e local |

### Mensageria

| Tool | O que faz |
|------|-----------|
| `get_inbox` | Conversas recentes |
| `get_conversation` | Uma conversa, por usuário ou thread |
| `search_conversations` | Busca mensagens por palavra-chave |
| `send_message` | Envia mensagem (exige confirmação) |

### Conexões

| Tool | O que faz |
|------|-----------|
| `connect_with_person` | Envia convite ou aceita um recebido, com nota opcional |

### Edição do próprio perfil

| Tool | O que faz |
|------|-----------|
| `edit_profile_intro` | Nome, headline, localização, setor |
| `edit_profile_about` | Seção "Sobre" |
| `add_experience` | Experiência profissional |
| `add_education` | Formação acadêmica |
| `add_skill` | Competência |
| `add_certification` | Certificação |
| `add_project` | Projeto |
| `add_publication` | Publicação |
| `add_course` | Curso |
| `add_language` | Idioma, com nível de proficiência |
| `add_volunteer_experience` | Trabalho voluntário |
| `add_honor` | Prêmio ou reconhecimento |

> ⚠️ **As tools de edição alteram seu perfil de verdade e executam direto, sem pedir confirmação.** Confirme antes de chamar — e note que `edit_profile_about` **substitui** todo o texto anterior da seção.

### Sessão

| Tool | O que faz |
|------|-----------|
| `close_session` | Fecha o navegador e libera recursos |

## 🏗️ Arquitetura

O pacote de scraping é modular — cada responsabilidade no seu lugar:

```
linkedin_mcp_server/scraping/
├── extractor.py       # núcleo: navegação, extração, feed, perfil
├── base.py            # tipos, constantes e helpers puros
├── i18n.py            # aliases EN/PT-BR (fonte única de verdade)
├── dom.py             # seletores estruturais compartilhados
├── profile_editor.py  # ProfileEditMixin  — edição de perfil
├── discovery.py       # DiscoveryMixin    — busca, vagas, empresa
├── messaging.py       # MessagingMixin    — inbox, conversas, envio
├── connecting.py      # ConnectionMixin   — convites
└── connection.py      # análise de estado de conexão (independe de idioma)
```

`LinkedInExtractor` compõe os mixins; cada um declara os helpers do host em um bloco `if TYPE_CHECKING:`. Separação real de responsabilidades, sem mudar o comportamento em runtime.

### Diagnósticos embutidos

Quando uma operação de edição falha, a resposta traz o que o servidor realmente encontrou na página — sem depurar às cegas:

- `labels_seen` — controles do formulário, com tag e nome acessível
- `buttons_seen` — botões do modal, com texto, `aria-label` e estado
- `anchors_seen` — âncoras de edição presentes na página

## 🐍 Desenvolvimento

```bash
uv sync --group dev                 # dependências de desenvolvimento
uv run ruff check . --fix           # lint
uv run ruff format .                # formatação
uv run ty check                     # tipos
uv run pytest                       # testes
uv run pre-commit run --all-files   # tudo o que a CI roda
```

Servidor local com janela visível e log detalhado:

```bash
uv run -m linkedin_mcp_server --no-headless --log-level DEBUG
```

Outras formas de instalação (Docker, MCP Bundle para Claude Desktop, `uvx`) seguem as do projeto original — veja a [documentação upstream](https://github.com/stickerdaniel/linkedin-mcp-server#installation-methods).

## 🛡️ Segurança e uso responsável

- A sessão fica **só na sua máquina** (`~/.linkedin-mcp/profile/`); nada é enviado a terceiros.
- Nunca comite `cookies.json`, `.env` ou o diretório de perfil — já cobertos pelo `.gitignore`.
- Use de acordo com o [Contrato do Usuário do LinkedIn](https://www.linkedin.com/legal/user-agreement). **Acesso automatizado pode violar os termos e levar a restrições na conta.** Escrita (mensagens, convites, edição de perfil) é mais sensível que leitura: vá com calma, sem rajadas de ações.
- Ferramenta para uso pessoal, fornecida sem qualquer garantia.

## 🙏 Créditos

Este fork existe porque a base é boa. Crédito a quem construiu:

- **[stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server)** — projeto original: arquitetura de scraping, autenticação por sessão de navegador e as ferramentas de leitura, busca e mensageria.
- **[PR #314, de Gabrcodes](https://github.com/stickerdaniel/linkedin-mcp-server/pull/314)** — implementação original das ferramentas de edição de perfil.
- **[Safee-ullah1/linkedin-mcp-server](https://github.com/Safee-ullah1/linkedin-mcp-server)** — fork que aplicou a edição de perfil sobre a v4.17.0.
- **[FastMCP](https://gofastmcp.com/)** e **[Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)** — base técnica.

A atribuição formal e a lista completa de modificações deste fork estão no [NOTICE](NOTICE).

## 📄 Licença

[Apache License 2.0](LICENSE) — a mesma do projeto original, como exige a cláusula de obras derivadas. Uso livre, inclusive comercial, mantendo a atribuição e os avisos de licença.

---

Feito em Manaus 🌳 por [Alison Silva](https://github.com/DevAlissu).
