This is the beginning of a new project (x1-advisor).

I have been working on an 'CRM'-like web platform for startups and investors. 
It consists of a front-end called `x1-app` and a back-end called `x1-backend`.

These are both linked to the current workspace, and are located here (inspect at your liesure to understand the inner workings):
`/home/david/code/x1/dev/x1-backend`
`/home/david/code/x1/dev/x1-app`

The backend just defines a few AI workflows so far: an AI-driven startup pitch-deck evaluation (`/home/david/code/x1/dev/x1-backend/mastra/eval`).
And then 2 document import workflows defined in `/home/david/code/x1/dev/x1-backend/mastra/import`.

I made some previous attempts at a more ambitious "agentic copilot" that has full control over the app, but that presented some challenges...

I got bogged down in all the minute details of context management (managing memory, context bloat, tool-result replay, alot of the issues mentioned here: https://arize.com/blog/context-management-in-agent-harnesses/ and here: https://rlancemartin.github.io/2025/06/23/context_engineering/, here https://rlancemartin.github.io/2025/10/15/manus/ ) .

so I decided to start over and just start by implementing a "research agent" that allows the user to perform open-ended, multi-hop research in an open-ended, interactive chat session.  I want to avoid (or "offload") as much of that context engineering stuff as possible for building this research agent...
I know it can't be totally avoided, but I'd like to offload much of it because (a) it is a headache and (b) existing solutions probably can handle it better, with more sophistication, than my "hand-rolled" attempts probably could.

The goal for the research agent to have full access/visibility into the attached DB, which includes pointers to documents stored on google cloud storage (GCS), and also web search. The DB contains information about startups and their team members. Startups may also upload documents, like pitch-deck or other files, like business plans. Usually in PDF format, but these will be converted to text (markdown) and stored in GCS, and maybe also on the DB. Individuals may be startup team members, or investors, and they may also upload documents, like CVs (resumes), which are also converted to markdown.

BTW, I had a conversation with another ai agent about this, and the transcript is here: `/home/david/code/x1/dev/x1-advisor/docs/chats/chat2.md`.
Please read this!
I don't mean that should be taken as "gospel", but it is just another source of food for thought, and possible ideas and inspiration.
The one thing I would say is that I think I like the idea of trying the haystack approach, `prototype A`... or at least `prototype B` if you think that is far more compelling (than A).

Just for reference, much of the code from my previous, aborted attempts is here: `/home/david/code/x1/x1-link`.

Feel free to poke around, but don't let it bias you or lead you astray. Some useful resources in there might be things like this skill for connecting to and reading/understanding the x1 database (`/home/david/code/x1/x1-link/.claude/skills/database-connection`).  If useful, you could copy it to a local skill folder and modify.improve it in any way you see fit.

BTW, I have copied the `.env` file from x1-link, so we have full credentials, etc for accessing databse, etc. 
See for yourself: `/home/david/code/x1/dev/x1-advisor/.env`