# AI Usage Log: My "Engineer-in-the-Loop" Diary

> *Note: As requested by the challenge, here is a transparent log of my AI usage, the prompts I used, where the AI generated great boilerplate, and crucially, where I had to step in and make architectural overrides.*

I used **Claude / Anthropic (via an AI Coding Assistant)** as a pair programmer for this challenge. My general approach was to let the AI handle the raw scaffolding and boilerplate, while I focused on the architectural boundaries, edge cases, and systemic trade-offs.

Here is a breakdown of how the collaboration went:

### 1. Generating the Seed Data (Task 0)
* **My Prompt:** *"Read the simulated schema requirements and write a Python seed script. I need 50 companies, some churned, 300 users, and realistic ticket volumes."*
* **The AI's attempt:** The AI wrote a decent script using `faker`, but it used `executemany` to insert 10,000+ events one single row at a time. It also completely randomized churn behavior.
* **My Override:** I pushed back on the single-row inserts because they took almost 5 minutes to run. I refactored the code to build tuples in memory and used `psycopg2.extras.execute_values()` to batch insert 5,000 rows at once, dropping the execution time to ~5 seconds. I also added a "taper-off" effect to the churn events so the health scorer would actually have realistic drops to detect. I used `TRUNCATE CASCADE` here to guarantee local developer idempotence.

### 2. The Sidecar Sync Engine (Task 1)
* **My Prompt:** *"Write an incremental sync engine with watermarks and schema evolution handling for the sidecar."*
* **The AI's attempt:** The AI provided a strong foundation using a `sync_state` table for the high-water mark tracking.
* **My Override:** While technically correct, the AI's first draft bundled the `dim_accounts` refresh AND the `fact_events_daily` push into a single gigantic database transaction. I caught this and split them up. If the massive events upsert operation fails midway, I didn't want the dimensional account updates to roll back with it. I also explicitly requested `ON CONFLICT DO UPDATE` constraints instead of standard inserts to guarantee the sync would be idempotent if it crashed midway.

### 3. Health Scoring with Window Functions (Task 2)
* **My Prompt:** *"Implement the 5-component health score in SQL. Provide the logic for login recency, feature adoption, and ticket trends. Include a LAG window function to detect 15-point drops for the 'at_risk' flag."*
* **The AI's attempt:** The AI produced excellent JSONB structures for the component breakdown in Postgres.
* **The Bug I Caught:** The AI wrote a CTE (Common Table Expression) for the window function and tried to do `WHERE at_risk = TRUE` directly on an alias that hadn't been fully evaluated by the execution planner yet. I caught the SQL syntax error and refactored the alias scoping into an outer query so it would execute cleanly. 

### 4. Claude Summarizer & The API (Tasks 3 & 4)
* **My Prompt:** *"Build a Claude API summarizer for the at-risk accounts. I need strict caching so we don't blow through tokens, and build a FastAPI app to expose it."*
* **The AI's attempt:** The AI correctly scaffolded the FastAPI routes and the Anthropic API call blocks.
* **My Override:** The AI implicitly trusted that Claude would always return perfectly formatted JSON strings. As the engineer-in-the-loop, I know LLMs occasionally pad their responses with conversational text (e.g., *"Here is your JSON:"*). I stepped in and added an explicit `json.JSONDecodeError` safety fallback to log the raw text instead of crashing the Python process. I also implemented a SHA-256 prompt-hashing gate so if the inputs haven't changed, the API call never fires. 

### 5. Bonus: Unit Testing
* **My Prompt:** *"Write Pytest unit tests for the health scoring business logic."*
* **The AI's attempt:** The AI started writing complicated `unittest.mock` injection tests to simulate the PostgreSQL database environment.
* **My Override:** I stopped it. Mocking the database is brittle and makes refactoring miserable. I architected the python logic so the pure mathematical scoring rules were extracted into isolated Python functions. This allowed us to write 34 lightning-fast unit tests that require absolutely zero database connections.

### Final Thoughts
The AI was a fantastic accelerator for things like FastAPI boilerplate and typing out Faker variables. But the true value of the system—the `execute_values` bulk ingest, the robust CTE scoping, the JSON decoding safety net, and the caching strategy—required explicit human oversight and architectural pushback to make it production-ready. 
