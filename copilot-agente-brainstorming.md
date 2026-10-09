# Agente Sys Brainstorming (Copilot Studio): o que foi feito e como funciona

Situação em 08/10/2026. Agente: **Sys Brainstorming**, ambiente padrão do Copilot Studio.
Fluxo testado de ponta a ponta no painel "Testar seu agente", com uma ideia fictícia (painel de pedidos de férias do RH). Nada foi publicado e nada foi criado no Azure DevOps.

---

## 1. O que foi feito

### 1.1 Prompts (aba Ferramentas)
Foram criados 7 prompts. O prefixo/sufixo "da ideia" existe porque o nome "Filtro" já estava em uso no ambiente.

| Prompt | Entradas | Saída | Modelo |
|---|---|---|---|
| Resumir ideia | ideia, problema, publico, processo_atual, frequencia | Texto | GPT-4.1 mini |
| Filtro da ideia | resumo | JSON (3 notas, motivos, premissas, sugestão) | GPT-4.1 mini |
| Plano de Negócio da ideia | resumo, resultado_filtro, respostas_extras | Texto (7 seções) | GPT-4.1 mini |
| PRD da ideia | plano_negocio, ajustes | Texto (10 seções) | GPT-4.1 mini |
| Protótipo HTML da ideia | prd, html_anterior, comentarios | Texto (HTML) | GPT-5 chat |
| Pitch da ideia | plano_negocio, prd | Texto (7 seções) | GPT-4.1 mini |
| Comparar duplicidade de ideia | resumo, lista_epicos | JSON | GPT-4.1 mini |

- O Protótipo usa as cores do Guide.pdf: azul `#255ea5` e rosa `#da488d`. O cinza `#6b7280` foi escolha minha, porque o guia não define um cinza.
- O prompt do Filtro ainda tem o marcador `[COMPLETE A LISTA AQUI]` (ferramentas internas existentes).
- O prompt "Comparar duplicidade" existe, mas **não está ligado a nenhum tópico**.

### 1.2 Tópicos
Os tópicos já existiam (montados pelo outro agente). Foi feito:

| Tópico | Alteração |
|---|---|
| Inicializar | Conferido, sem mudança (zera as variáveis globais) |
| Entendimento da Ideia | Chama "Resumir ideia" e redireciona para o Filtro depois do "Sim" |
| Filtro e Plano de Negócio | Troquei os prompts antigos pelos novos (Filtro e Plano). Redireciona para o PRD depois do plano aprovado |
| PRD | Chama "PRD da ideia". Se o usuário pede ajuste, regenera. Redireciona para o Protótipo |
| Protótipo | Chama "Protótipo HTML da ideia". Em ajuste de escopo, regenera também o PRD. Redireciona para o Pitch |
| Pitch | Chama "Pitch da ideia". Redireciona para a Revisão Final |
| Voltar Etapa | Cada etapa escolhida (1, 3, 4, 5, 6) redireciona para o tópico correspondente |
| Revisão Final | Não alterado. Mostra o cartão com autor, área, notas e versão |

### 1.3 Defeitos encontrados no teste e corrigidos
1. **Fluxo parava no primeiro passo:** nenhum tópico redirecionava para o seguinte e o agente caía numa busca no Guide.pdf. Corrigido com os redirecionamentos acima.
2. **Erro "AIModelActionBadRequest":** os prompts rejeitam entrada em branco. Corrigido enviando "Nenhum ajuste solicitado" e "Nenhuma versao anterior" quando não há ajuste ou versão anterior.

---

## 2. Passo a passo do agente (como a conversa acontece)

O agente só avança com a aprovação do usuário. Valores sem fonte aparecem como "⚠️ Premissa a validar".

| # | Etapa | O que o usuário vê/faz | Tópico / prompt | Estado |
|---|---|---|---|---|
| 1 | Entendimento | Responde 6 perguntas (ideia, problema, público, como é feito hoje, frequência, área). Lê o resumo e confirma "Sim/Não" | Entendimento da Ideia, Prompt "Resumir ideia" | Funcionando |
| 2 | Duplicidade | O agente compara com Épicos existentes | Prompt "Comparar duplicidade", Fluxo A | **Não ligado** |
| 3 | Filtro | Vê as 3 notas (ideia, alinhamento, ROI) com motivos. Se alguma nota < 3, recebe sugestão e escolhe ajustar ou encerrar | Filtro e Plano, Prompt "Filtro da ideia" | Funcionando |
| 3b | Plano de Negócio | Lê o plano de 7 seções e aprova | Filtro e Plano, Prompt "Plano de Negócio da ideia" | Funcionando |
| 4 | PRD | Lê o PRD de 10 seções e aprova ou pede ajuste | PRD, Prompt "PRD da ideia" | Funcionando |
| 5 | Protótipo | Recebe o protótipo (v1, v2...) e aprova ou pede mudança. Se muda o escopo, o PRD é atualizado | Protótipo, Prompt "Protótipo HTML da ideia" | Gera o HTML, **mas o link está vazio** (falta o Fluxo C) |
| 6 | Pitch | Lê o pitch de uma página e aprova | Pitch, Prompt "Pitch da ideia" | Funcionando |
| 7 | Revisão final | Vê o resumo de tudo e confirma criação ou volta a uma etapa | Revisão Final | Cartão funcionando |
| 8 | Criação no Azure DevOps | Épico com 3 a 4 Features, PRD e protótipo anexados, aviso ao comitê no Teams | Tópico 8, Fluxo B | **Não existe** |
| 9 | Avaliação do comitê | André, Germano e Gabriel decidem | Manual no ADO | Fora do agente |
| 10 | Retorno ao autor | Autor recebe a decisão com justificativa | Fluxo D | **Não existe** |

"Voltar a qualquer etapa": o tópico Voltar Etapa pergunta o número da etapa e redireciona.

---

## 2b. Novidades de 08/10/2026 (segunda sessão)

- **Pergunta inicial "perfil técnico?"** no tópico Entendimento da Ideia, antes da primeira pergunta. Resposta guardada em `Global.EhTecnico` (sim/não).
- **Pular o PRD para quem não é técnico:** no início do tópico PRD, se `Global.EhTecnico = false`, o agente copia o Plano de Negócio para `Global.PRD` (assim o Protótipo e o Pitch recebem texto e não ficam em branco), avisa o usuário e vai direto ao Protótipo. Quem é técnico segue o fluxo normal. O motivo exato do pulo (frase do pedido cortada) foi assumido: o PRD é técnico demais para esse perfil. Ajustar se for outro.
- **Fluxo C "Salvar protótipo" publicado** (OneDrive for Business, conexão da conta do próprio usuário). Ligado ao tópico Protótipo: entradas html, versão e título (`Prototipo-<data-hora>`); saída `link` gravada em `Global.LinkPrototipo`. **Ainda não testado em conversa.**
- Pontos conhecidos: a mensagem "Agora vou escrever o PRD" aparece antes do pulo para quem não é técnico; se o usuário pedir mudança de escopo no Protótipo, o agente ainda regenera um PRD (não é mostrado). A conexão do OneDrive é pessoal: para o piloto o ideal é uma conta de serviço.

---

## 3. O que falta

**Para construir (depende de acessos):**
- Fluxo C: salvar o HTML no SharePoint e devolver o link.
- Fluxo A: listar Épicos no ADO, para a checagem de duplicidade.
- Fluxo B e tópico 8: criar Épico e Features, anexar PRD e HTML, avisar o comitê.
- Fluxo D: avisar o autor da decisão.
- Lista de ferramentas existentes no prompt do Filtro.

**Para pedir a outras pessoas:**
- Admin do ADO: time e board, campos personalizados, estados Aprovada / Aprovada com ajustes / Não aprovada, tag `sys-brainstorming`, permissão.
- TI: conta de serviço (service principal) ou decisão de usar a conta do usuário.
- Admin do Power Platform: política de dados (DLP) e licenciamento.
- Admin do SharePoint: pasta `/Brainstorming/Prototipos` e links para a organização.
- Admin do Teams: publicação do agente e canal do comitê.
- Comitê: nota mínima, cadência, Features já concluídas ou não.

**Pontos de atenção:**
- Testados só o caminho feliz (todas as respostas "Sim"). Os caminhos de ajuste, de reprovação no filtro e o Voltar Etapa em conversa não foram testados.
- O HTML do protótipo não foi aberto, então a qualidade visual não foi avaliada.
- O agente não pode ser considerado pronto para o piloto até existir a criação no ADO.
