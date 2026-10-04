# linkedin-mcp-server (fork em português)

Servidor MCP que conecta assistentes de IA, como o Claude, ao LinkedIn pela sua própria sessão de navegador. Lê perfis, empresas e vagas, busca pessoas e conversas e envia mensagens com confirmação. Este fork existe para que o servidor funcione bem em contas com a interface em português e para levar a ele a edição do próprio perfil, com segurança.

> Projeto independente, sem afiliação, autorização, endosso ou patrocínio da LinkedIn Corporation ou da Microsoft. "LinkedIn" é marca registrada da LinkedIn Corporation, usada aqui apenas para identificar o serviço com o qual este software interopera.

## Estado do projeto

Esta branch, `nova-base`, é a reconstrução do fork sobre a versão 4.26.2 do projeto original, [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server). A versão anterior, construída sobre a 4.17.0, continua na branch `main` e na release [v4.17.0-br.1](https://github.com/DevAlissu/linkedin-mcp-server/releases/tag/v4.17.0-br.1). Ela tem 12 ferramentas de edição de perfil e a camada de tradução para português, mas ficou para trás em proteções que o original ganhou nos últimos meses.

Ao partir da 4.26.2, esta base já traz essas proteções:

- **Conta restrita:** o servidor reconhece a tela de restrição e para todas as ferramentas, em vez de ficar esperando um login que nunca chega.
- **Leitura vazia:** quando a página volta vazia por limite do LinkedIn, o agente é avisado para parar, e não para tentar de novo.
- **Mais de um cliente MCP:** o perfil do navegador é coordenado entre processos, então dois clientes abertos ao mesmo tempo não apagam a sessão um do outro.
- **Envio de mensagem:** só conta como enviado depois que o próprio LinkedIn confirma.
- **Correções:** convites e busca de vagas depois das mudanças de interface de setembro de 2026.
- **Versões:** FastMCP 4 e a especificação MCP 2026-07-28.

A edição de perfil e a tradução para português ainda não estão nesta branch. Elas vão ser portadas na próxima fase, num desenho com confirmação humana feita pelo próprio servidor.

## Plano

1. **Base nova (esta branch).** Versão 4.26.2 com a identidade do fork e sem os blocos patrocinados do README original. Depois, uso real com uma conta em português para registrar o que quebra.
2. **Escrita com confirmação.** Toda ferramenta que escreve na conta passa a mostrar uma prévia montada pelo servidor e a exigir confirmação do usuário antes de executar. A edição de perfil volta nesse desenho, com releitura de cada campo depois de salvo, e a tradução para português passa a ser uma tabela por idioma.
3. **Ferramentas de carreira.** Comparar uma vaga com o perfil e propor as mudanças, importar a exportação oficial de dados do LinkedIn e acompanhar candidaturas localmente. Também entram etiquetas de regime (CLT, PJ, contractor), de modelo remoto e de exigência de inglês.
4. **Canais oficiais e testes.** Publicação de posts pela API oficial, com o aplicativo do próprio usuário, e testes de segurança contra instruções escondidas em conteúdo de terceiros.
5. **Contribuição com o projeto original.** Suporte a português em pequenas partes, no formato que o mantenedor pede.

## Riscos

O Contrato do Usuário do LinkedIn proíbe acesso automatizado não autorizado, inclusive à própria conta, e o LinkedIn pode limitar ou restringir contas que usam esse tipo de ferramenta. O risco é de quem usa. Este fork é pensado para uso pessoal, local, com volume de uso humano, e não inclui nem recomenda técnicas para evitar detecção, proxies para contornar limites, segunda conta ou coleta de dados de terceiros em massa. Quando o LinkedIn sinalizar restrição, limite ou verificação, o certo é parar e resolver no navegador.

## Instalação a partir do código

Requer [Git](https://git-scm.com/downloads) e [uv](https://docs.astral.sh/uv/). O navegador usado pela automação é baixado pelo próprio servidor no primeiro uso.

```bash
git clone https://github.com/DevAlissu/linkedin-mcp-server.git
cd linkedin-mcp-server
git checkout nova-base
uv sync
```

Para entrar na conta, escolha uma das duas formas. A primeira abre uma janela para você fazer login. A segunda reaproveita a sessão de um navegador baseado em Chromium (Chrome, Edge, Brave e outros) em que você já esteja logado no LinkedIn.

```bash
uv run mcp-server-linkedin --login
uv run mcp-server-linkedin --import-from-browser
```

A sessão fica em `~/.linkedin-mcp/profile/` e é reutilizada nas próximas execuções. Para conferir se ela continua válida, use `uv run mcp-server-linkedin --status`.

Para registrar no Claude Code:

```bash
claude mcp add linkedin -- uv run --directory /caminho/para/linkedin-mcp-server mcp-server-linkedin
```

Em outros clientes MCP, como o Claude Desktop:

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

O servidor é um processo de vida longa. Depois de atualizar o código, reinicie a conexão MCP (no Claude Code, `/mcp` e reconectar) para a versão nova valer.

## Ferramentas

| Ferramenta | O que faz |
|---|---|
| `get_person_profile` | Lê seções de um perfil, como experiência, formação, competências, projetos e posts |
| `get_my_profile` | Lê o seu próprio perfil, com as mesmas seções |
| `connect_with_person` | Envia ou aceita convite de conexão, com nota opcional |
| `get_sidebar_profiles` | Lista os perfis recomendados na lateral de um perfil |
| `get_inbox` | Lista as conversas recentes da caixa de mensagens |
| `get_conversation` | Lê uma conversa por usuário ou por identificador da conversa |
| `search_conversations` | Busca mensagens por palavra-chave |
| `send_message` | Envia mensagem depois de confirmação |
| `get_company_profile` | Lê dados, posts e vagas de uma empresa |
| `get_company_posts` | Lê os posts recentes da página de uma empresa |
| `search_companies` | Busca empresas por palavra-chave |
| `get_company_employees` | Lista funcionários de uma empresa, com filtro opcional |
| `search_jobs` | Busca vagas por palavra-chave e local |
| `get_saved_jobs` | Lista as vagas que você salvou |
| `search_people` | Busca pessoas por palavra-chave, local, grau de conexão ou empresa |
| `get_job_details` | Lê os detalhes de uma vaga |
| `get_feed` | Lê os posts recentes do seu feed |
| `search_posts` | Busca posts por palavra-chave, com filtro de data opcional |
| `close_session` | Fecha o navegador e libera os recursos |

A documentação completa das opções de linha de comando, de Docker e de solução de problemas está no [README do projeto original](../README.md), em inglês.

## Créditos e licença

Este fork é uma obra derivada do [linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server), de Daniel Sticker e colaboradores, e segue a mesma licença, [Apache 2.0](../LICENSE). As atribuições e a lista de modificações deste fork estão no [NOTICE](../NOTICE).

Mantido por [Alison Silva](https://github.com/DevAlissu).
