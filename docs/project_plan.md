# AI News Aggregator: Detailed Project Plan & Architecture

> **Status:** original design spec (2026-09-27), kept as-is. This file is *not*
> updated as the project evolves — for current state see
> [`README.md`](../README.md) (usage) and [`plan.md`](./plan.md) (progress/changelog);
> technical discoveries are in [`findings.md`](./findings.md).

## 1. System Overview

A robust, local-first web application designed to ingest large volumes of RSS feeds, process them using AI to group related articles into chronological "Stories," summarize content, assess quality, and rank the stories based on an evolving user interest profile.

### Core Components & Tech Stack

*   **Backend:** Python (FastAPI). Chosen for its native asynchronous capabilities, which are essential for I/O-bound tasks like concurrently fetching dozens of RSS feeds and interacting with HTTP-based LLM APIs.
*   **Database:** **SQLite with the `sqlite-vec` extension** acting as a unified relational and vector database. This entirely eliminates network overhead, avoids data synchronization bugs between a traditional DB and a standalone vector DB (like ChromaDB), and allows the backend to remain extremely lightweight in a single portable file.
*   **AI Engine (Local via Ollama):**
    *   **Text/Chat Model:** Local Ollama instance (`qwen3:27b`) via HTTP (http://127.0.0.1:11434/v1) for summarization, classification, and scoring.
    *   **Embedding Model:** `qwen3-embedding:0.6b-CPU` via local Ollama for semantic search and clustering. Fast enough for CPU generation while providing high-quality dense vectors.
*   **Frontend:** A modern, lightweight Single Page Application (SPA) built with Vanilla HTML/CSS/JS. It will utilize the native `Fetch API` and Web Components to encapsulate UI logic without the overhead of heavy frameworks like React or Vue.

## 2. Data Models (Database Schema)

The database will utilize standard SQLite tables with a joined virtual table for `sqlite-vec` embeddings.

*   **`Feed`**: 
    *   `id` (PK), `title`, `xml_url`, `site_url`, `category` (mapped from OPML folder structure), `last_fetched_at`.
*   **`Story`**:
    *   `id` (PK), `headline` (AI generated), `summary` (AI generated, rolling updates).
    *   `tags` (JSON array).
    *   `metrics`: (JSON Object) e.g., `{"urgency_level": 8, "impact_scale": "Global", "status": "Ongoing"}`.
    *   `quality_score` (Numeric 1.0 - 10.0).
    *   `created_at`, `updated_at`. (Indexed heavily for time-decay calculations).
*   **`Article`**: 
    *   `id` (PK), `feed_id` (FK), `story_id` (FK), `title`, `url`, `author`.
    *   `content_raw` (Text), `hero_image_url` (Text, for future Vision AI/UI).
    *   `published_date`, `created_at`.
*   **`Article_Embeddings` (sqlite-vec Virtual Table)**:
    *   `article_id` (Row ID), `embedding` (FLOAT BLOB representing the `qwen3` vector).
*   **`UserProfile`**:
    *   `id` (PK - typically just one local user).
    *   `tag_weights`: (JSON Object) e.g., `{"technology": 5.5, "politics": -2.0, "ai": 8.1}`.
    *   `source_weights`: (JSON Object) Publisher preferences based on feedback.
    *   `preferences`: (JSON Object) e.g., `{"min_quality": 5.0, "summary_length": "medium"}`.

## 3. The AI Processing Pipeline (The "Brain")

A background worker (e.g., using `apscheduler` or Celery, though FastAPI `BackgroundTasks` might suffice initially) runs on an interval to trigger the pipeline:

### Step 1: Ingestion & Initial Assessment

*   **Parse RSS Feeds:** Concurrently fetch feeds using `feedparser`. Respect `ETag` and `Last-Modified` headers to minimize redundant bandwidth.
*   **Full-Text Extraction Fallback Chain:** RSS descriptions are notoriously incomplete. Best practice dictates spoofing a standard browser User-Agent to avoid 403 blocks.
    1.  Try `trafilatura` (highly accurate for main body text, strips ads/nav).
    2.  If it fails or returns < 100 words, fallback to `newspaper3k`.
    3.  If that fails, fallback to `BeautifulSoup` basic paragraph extraction.
*   **AI Prompt 1 (Article Level):** The LLM evaluates the extracted raw article text to extract:
    *   Standardized Tags/Categories.
    *   Quality/Seriosity Score (1-10 scale assessing factual basis, source citations, and lack of sensationalism).

### Step 2: Story Clustering (Vector Search)

*   **Embedding Generation:** Pass the article text (truncated to the first ~1000 words to ensure CPU embedding via `qwen3-embedding:0.6b-CPU` executes in seconds) to Ollama to generate vector embeddings.
*   **Vector Search & KNN:** Insert the vector into the `sqlite-vec` table. To cluster, execute a query using `sqlite-vec`'s **KNN (k-nearest neighbors)** function. 
    *   *Crucial Constraint:* Combine the KNN search with a strict date filter via a `JOIN` on the `Story` table (e.g., `WHERE Story.updated_at >= datetime('now', '-48 hours')`). This prevents the algorithm from endlessly scanning years of historical news and falsely attaching a new Apple product launch to an article from 2022.
*   **Decision:** 
    *   If **Existing (High Cosine Similarity)**: Attach the new `Article` to the matching `Story`.
    *   If **New (Low Similarity / No Match)**: Create a new `Story` record.

### Step 3: Story Update & Context Management

*   **AI Prompt 2 (Story Level):** Update the consolidated Story Summary and assess `metrics` (urgency, impact).
*   **Context Strategy (Full Context Preference):** Pass the full raw text of all attached articles to generate a high-fidelity summary.
*   **Context Compression (Fallback Only):** If the combined article text threatens the LLM's context window (~80k tokens), trigger a Map-Reduce compression: the LLM summarizes older articles into dense bullet points, appending only the newest articles in full text.
*   **Urgency Notification:** If `urgency_level` >= 8 AND `status` == "Breaking", emit a Server-Sent Event (SSE) or WebSocket message to the frontend to trigger a push notification.

## 4. Ranking & User Personalization Algorithm

The backend calculates a dynamic `Display_Score` for every active story requested by the frontend.

*   **The Algorithm:** 
    `Display_Score = (Urgency_Level * W1) + (Quality_Score * W2) + (User_Interest_Score * W3) - (Time_Decay_Penalty)`
    *   *Time Decay:* Uses an exponential decay function based on the hours since the story's `updated_at` timestamp.
*   **User Interest Score:** Calculated by matching the Story's generated tags against the User's `tag_weights` (dot product of the matching JSON keys).
*   **Feedback Loop:** Interactions (👍 / 👎, dwell time/reading, dismissing) hit a REST endpoint. The backend uses a learning rate (e.g., $\alpha = 0.1$) to adjust the `tag_weights` incrementally, preventing a single click from completely altering the feed.

## 5. Frontend UI/UX Flow

Built with zero dependencies, utilizing native CSS Grid/Flexbox and Web Components.

1.  **Top Section: "Quick Heads Up"**
    *   A dynamically generated AI paragraph (updated every few hours) summarizing the top 3-5 most critical stories.
2.  **Main Feed: Ranked Stories**
    *   Virtualized list (to save memory) displaying Story cards, sorted by `Display_Score`.
    *   *Card UI:* AI Headline, consolidated summary, Quality Badge (color-coded), and Tag pills.
    *   *Expandable UI:* Clicking expands an accordion to reveal the timeline of individual source articles and direct links.
3.  **Interaction Bar (Per Story):**
    *   Explicit feedback: 👍 (Increase tag weights) / 👎 (Decrease tag weights).

## 6. Implementation Phases

*   **Phase 1: Backend Foundation & Database (The Core)**
    *   Install dependencies: FastAPI, `feedparser`, `trafilatura`, `httpx`.
    *   **Crucial Step:** Compile and install the `sqlite-vec` extension for Python. This requires specific C-bindings so the Python `sqlite3` library can load the extension via `db.enable_load_extension(True)` and `db.load_extension("vec0")`.
    *   Initialize the SQLite database with strict foreign keys and the virtual vector tables.
    *   Write the OPML parser to ingest `subscriptions-2026-09-27.opml`.
*   **Phase 2: Ingestion & AI Interfacing**
    *   Build the RSS fetcher loop with the fallback extraction chain.
    *   Implement HTTP clients to communicate with the local Ollama instance for Prompt 1 (Extraction) and embeddings (`qwen3`).
*   **Phase 3: Clustering & Summarization Logic**
    *   Implement the `sqlite-vec` KNN query with the 48-hour time constraint.
    *   Develop the prompt engineering for Prompt 2 (Story Summary) and context compression rules.
*   **Phase 4: API & Scoring Algorithm**
    *   Build the math behind the `Display_Score` and the feedback loop endpoints for adjusting user weights.
    *   Expose FastAPI routes (e.g., `GET /api/stories`, `POST /api/feedback`).
*   **Phase 5: Frontend Development & Tuning**
    *   Build the HTML/JS Web Components.
    *   Run end-to-end tests locally, tuning the `Display_Score` weights (W1, W2, W3) and the embedding similarity threshold for optimal clustering.
