# Prompts do agente Brainstorming SysManager (Copilot Studio)

Como usar: em Tools → Add a tool → New prompt, crie um prompt por seção abaixo.
Crie primeiro as **Entradas (Inputs)** com os nomes exatos (tipo Text). No texto, onde aparecer
`{nome}`, apague o `{nome}` e insira a entrada correspondente (digite `/` ou use o botão
"+ Add content" → Inputs). Entradas opcionais: deixe um valor de exemplo vazio.

---

## Prompt 1: Resumir ideia
**Entradas:** ideia, problema, publico, processo_atual, frequencia
**Modelo:** padrão
**Saída:** Text

```
Resuma a ideia abaixo em no máximo 5 linhas, em português do Brasil simples, sem inventar nenhuma informação.
Use só o que foi dito. Se algo não foi informado, não complete.

Ideia: {ideia}
Problema: {problema}
Quem tem o problema: {publico}
Como é feito hoje: {processo_atual}
Frequência: {frequencia}

Responda apenas com o resumo.
```

---

## Prompt 2: Filtro
**Entradas:** resumo
**Modelo:** padrão
**Saída:** JSON

```
Você avalia ideias de ferramentas internas da SysManager (uso interno, não produtos para clientes externos).
Seja honesto e respeitoso. Escreva em português do Brasil.

Ferramentas que já existem na SysManager: Plataforma Unik, Microsoft PO, Azure DevOps, BIs do Data Lake (Power BI), [COMPLETE A LISTA AQUI].

Avalie de 1 a 5 (números inteiros), explicando o motivo de cada nota:
1. A ideia é boa? O problema é real, frequente e relevante?
2. Faz sentido para a SysManager? Está alinhada às prioridades da empresa e NÃO duplica ferramentas existentes?
3. Qual o possível ROI? Existe ganho mensurável em horas, custo, erro ou risco?

Regras:
- Nunca invente números. Se faltar um dado, liste em "premissas" como algo a validar.
- Se a ideia duplica uma ferramenta existente, a nota de alinhamento deve ser menor que 3 e o motivo deve citar a ferramenta.
- Se for um produto para cliente externo, dê nota 1 em alinhamento e explique que está fora do escopo.
- Se alguma nota for menor que 3, preencha "sugestao_reformulacao" com uma sugestão prática e gentil. Se todas forem 3 ou mais, deixe vazio.

Ideia: {resumo}

Responda SOMENTE com este JSON, sem texto antes ou depois e sem ```:
{"nota_ideia":0,"motivo_ideia":"","nota_alinhamento":0,"motivo_alinhamento":"","nota_roi":0,"motivo_roi":"","premissas":[],"sugestao_reformulacao":""}
```

---

## Prompt 3: Plano de Negócio
**Entradas:** resumo, resultado_filtro, respostas_extras (opcional)
**Modelo:** padrão
**Saída:** Text

```
Escreva o Plano de Negócio da ideia abaixo, em português do Brasil, em Markdown, com estas 7 seções, nesta ordem:

## 1. Problema e oportunidade
## 2. Público impactado
## 3. Como é feito hoje e quanto custa
## 4. Solução proposta
## 5. Estimativa de ROI
(horas economizadas por mês, custos evitados e esforço de construção: Pequeno, Médio ou Grande)
## 6. Riscos e dependências
## 7. Notas do filtro
(as três notas com o motivo de cada uma)

Regras:
- Use apenas as informações fornecidas. Nunca invente números.
- Todo valor sem fonte deve aparecer como "⚠️ Premissa a validar".
- Não use dados sensíveis (salários, dados pessoais, dados de clientes).
- Linguagem simples e direta.

Resumo da ideia: {resumo}
Resultado do filtro (JSON): {resultado_filtro}
Informações extras dadas pelo usuário: {respostas_extras}
```

---

## Prompt 4: PRD
**Entradas:** plano_negocio, ajustes (opcional)
**Modelo:** o mais forte disponível
**Saída:** Text

```
Escreva o PRD (Documento de Requisitos de Produto) a partir do Plano de Negócio abaixo, em português do Brasil, em Markdown, com estas 10 seções, nesta ordem:

## 1. Visão geral
## 2. Problema
## 3. Objetivos e indicadores de sucesso
## 4. Usuários
## 5. Escopo do MVP
## 6. Fora do escopo
## 7. Requisitos funcionais
(lista numerada: RF01, RF02...)
## 8. Requisitos não funcionais
(segurança, dados sensíveis, integrações)
## 9. Fluxo principal do usuário
(passo a passo numerado, pensando em 3 a 5 telas)
## 10. Premissas e dúvidas em aberto

Regras:
- Use só o que está no Plano de Negócio. Nunca invente números; valores sem fonte ficam como "⚠️ Premissa a validar".
- Não inclua dados sensíveis.
- O fluxo principal da seção 9 será usado para gerar um protótipo, então seja concreto sobre o que o usuário vê e faz em cada passo.
- Se o campo de ajustes abaixo vier preenchido, devolva o PRD COMPLETO já com os ajustes aplicados (não devolva só as mudanças).

Plano de Negócio: {plano_negocio}
Ajustes pedidos (se houver): {ajustes}
```

---

## Prompt 5: Protótipo HTML
**Entradas:** prd, html_anterior (opcional), comentarios (opcional)
**Modelo:** o mais forte disponível
**Saída:** Text

Antes de colar, troque os `#XXXXXX` pelos HEX do Guide.pdf.

```
Gere um protótipo de alta fidelidade em UM único arquivo HTML (HTML, CSS e JavaScript juntos), responsivo, com 3 a 5 telas do fluxo principal do MVP descrito no PRD. A navegação entre telas é feita via JavaScript, sem recarregar a página.

Design System SysManager:
- Fonte: Montserrat (Google Fonts), peso medium para texto e bold para títulos
- Cor primária (azul): #255ea5
- Cor de destaque (rosa): #da488d
- Cinza (textos secundários e bordas): #6b7280 (o guia não define um cinza em HEX; ajuste se houver um oficial)
- Variações permitidas: azul marinho #003478 e azul celeste #6ba1d8 como apoio, sempre combinados com o azul e o rosa principais
- Fundo claro, cantos arredondados de 8px, bastante espaço em branco
- O texto "SysManager" aparece no cabeçalho (nunca traduza o nome)

Regras:
- Apenas dados fictícios. Nunca use dados reais (nomes de pessoas reais, clientes, salários).
- Todo texto da interface em português do Brasil.
- Mostre apenas as telas do fluxo principal do MVP, nada fora do escopo do PRD.
- Se houver versão anterior e ajustes, parta da versão anterior e aplique somente os ajustes pedidos, mantendo o resto.
- Responda SOMENTE com o código HTML, começando em <!DOCTYPE html>, sem explicações e sem ```.

PRD: {prd}
Versão anterior (se houver): {html_anterior}
Ajustes pedidos (se houver): {comentarios}
```

---

## Prompt 6: Pitch
**Entradas:** plano_negocio, prd
**Modelo:** padrão
**Saída:** Text

```
Escreva o pitch da ideia para o comitê de validação, em português do Brasil, em Markdown, com no máximo uma página (cerca de 300 palavras), linguagem direta para executivo, com estas 7 seções:

1. A ideia em uma frase
2. O problema
3. A solução
4. Quem se beneficia
5. ROI estimado
6. O que é preciso para começar
7. O que pedimos ao comitê

Regras:
- Use só o que está no Plano de Negócio e no PRD. Nunca invente números; valores sem fonte ficam como "⚠️ Premissa a validar".
- Sem jargão técnico desnecessário.

Plano de Negócio: {plano_negocio}
PRD: {prd}
```

---

## Prompt 7: Comparar duplicidade
**Entradas:** resumo, lista_epicos (JSON vindo do Flow A)
**Modelo:** padrão
**Saída:** JSON

```
Compare a ideia nova com a lista de Épicos já registrados e diga se algum tem proposta parecida (mesmo problema ou mesma solução, mesmo que escrito com outras palavras).

Ideia nova: {resumo}
Épicos existentes (JSON): {lista_epicos}

Regras:
- Considere parecido apenas quando o problema e a solução forem realmente semelhantes. Temas vizinhos não contam.
- Em "motivo", explique em uma frase por que é parecido.
- Se a lista estiver vazia ou nenhum for parecido, responda existe_parecido false e epicos [].

Responda SOMENTE com este JSON, sem texto antes ou depois e sem ```:
{"existe_parecido":false,"epicos":[{"id":"","titulo":"","motivo":""}]}
```

---

## Dicas
- No Prompt 2 e no 7, use Output: **JSON**. Nos outros, **Text**.
- Se o editor reclamar das chaves `{ }` do JSON de exemplo, deixe como está: só os nomes de entradas inseridos pelo botão são tratados como variáveis.
- Clique em **Test** em cada prompt com dados de exemplo antes de ligar no tópico.
