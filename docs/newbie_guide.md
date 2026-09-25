# 🐱 Newbie Developer Guide: Navigating & Hacking DaCat

Welcome to the **DaCat** codebase! If you are a new or beginner developer feeling overwhelmed by all the files, tests, and directories, **this guide is for you**.

---

## 🧭 The 30-Second Mental Model

At its core, **DaCat** is composed of four main parts:

```
┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐
│   Discord Bot   │ <---> │ SQLite Database │ <---> │  Web Dashboard  │
│    (bot.py)     │       │    (bot.db)     │       │ (keep_alive.py) │
└─────────────────┘       └─────────────────┘       └─────────────────┘
        │                                                    │
        ▼                                                    ▼
┌─────────────────┐                                 ┌─────────────────┐
│  Feature Cogs   │                                 │ Supabase Cloud  │
│    (cogs/)      │                                 │ (cloud_sync.py) │
└─────────────────┘                                 └─────────────────┘
```

1. **The Discord Bot (`bot.py`)**: Connects to Discord, registers slash commands, and listens for user interactions.
2. **The Feature Cogs (`cogs/`)**: Modular python files containing commands (e.g. `economy.py`, `games.py`, `tickets.py`).
3. **The SQLite Database (`bot.db` + `utils/db.py`)**: Stores server settings, balances, inventory, tickets, etc.
4. **The Web Dashboard (`keep_alive.py` + `console/`)**: A Flask web server that provides a browser dashboard, health check (`/health`), and keeps the bot awake on hosting providers.

---

## 🗂️ What Does Every File and Folder Do?

Here is a plain-English map of the repository so you always know where things belong:

### Core Application Files
| File | What It Does |
|---|---|
| [`bot.py`](../bot.py) | **The Main Entrypoint**. Boots up the bot, connects to Discord Gateway, initializes the database, and loads all cogs. |
| [`keep_alive.py`](../keep_alive.py) | **Web Server & Dashboard**. A Flask application running on port 8080 that serves web pages, API routes, and `/health`. |
| [`force_sync.py`](../force_sync.py) | **Command Sync Script**. A utility you can run manually if Discord slash commands are ever out of sync. |

### Directories
| Directory | Purpose |
|---|---|
| [`cogs/`](../cogs/) | **Commands & Features**. Each file here is a distinct feature module (Economy, Games, Moderation, Tickets, etc.). |
| [`utils/`](../utils/) | **Helper Functions**. The engine room! Handles database queries (`db.py`), styles and embeds (`style.py`), permissions (`permissions.py`), and more. |
| [`tests/`](../tests/) | **Automated Tests**. Over 2,100 automated checks ensuring changes don't break the bot or database. *(See below!)* |
| [`console/`](../console/) | **Web Console Source**. Frontend TypeScript/Vite application for the web dashboard. |
| [`templates/`](../templates/) | **HTML Templates**. Jinja2 templates served by `keep_alive.py`. |
| [`static/`](../static/) | **Web Assets**. CSS, JavaScript, and images for the web dashboard. |
| [`docs/`](../docs/) | **Documentation**. Deep-dive architectural guides and developer notes. |
| [`deleted_features/`](../deleted_features/) | **Retired Code Archive**. An intentional archive of older retired features (like music or giveaways). Not loaded by the bot, kept in case you ever want to revive them. |

### Configuration & Deployment Files
| File | Purpose |
|---|---|
| [`requirements.txt`](../requirements.txt) | Lists the external Python libraries needed (`discord.py`, `flask`, `pytest`, etc.). |
| [`.env.example`](../.env.example) | Example environment file showing what secret keys (like `DISCORD_TOKEN`) you need. |
| [`.env`](../.env) | Your **real local secrets**. Never committed to git. |
| [`Dockerfile`](../Dockerfile) | Instructions to run the bot inside a Docker container. |
| [`Procfile`](../Procfile) | Tells cloud hosts like Render or Heroku how to start the bot (`worker: python bot.py`). |
| [`render.yaml`](../render.yaml) | Cloud deployment blueprint for Render.com. |
| [`AI_POLICY.md`](../AI_POLICY.md), [`PRIVACY.md`](../PRIVACY.md), [`TERMS.md`](../TERMS.md) | Required policies for Discord verification and safety. |

---

## 🧪 "What Are All These Test Files in `tests/`?"

When you open `tests/`, you will see about 80 `test_*.py` files. That can look scary at first! Here is why they are there and why they are your best friend:

### 1. What is an automated test?
Instead of having to:
1. Start the bot
2. Open Discord
3. Type `/coinflip`
4. Check if coins were awarded
5. Repeat 50 times for every feature...

A test is a Python script that simulates running a command or database query in **0.01 seconds** to verify it works.

### 2. Why are there so many test files?
There are over 2,150 automated tests in this project. Each file tests a specific area:
- `test_economy.py` tests balances, transfers, and shop purchases.
- `test_company.py` tests the voice companion logic.
- `test_permissions.py` verifies regular members cannot run admin commands.
- `test_db_isolation.py` verifies server A cannot access server B's data.

### 3. Should I delete any test files?
**No!** Tests are your safety net. If you edit a database query or change a function in `utils/`, running tests tells you immediately if you broke anything *before* you deploy to Discord.

### 4. How do I run tests as a developer?
You don't need to run all 2,000+ tests every time. You can run just the test file for the feature you are working on:

```bash
# Run tests for a single file (Super fast! Takes < 1 second):
.venv/bin/pytest tests/test_games_money.py

# Run only one specific test function:
.venv/bin/pytest tests/test_company.py -k "test_the_card_says_how_to_turn_it_off"

# Run the entire test suite:
.venv/bin/pytest
```

---

## ✍️ How to Read & Write Code Here (Beginner Walkthrough)

### Scenario: "I want to add a new slash command!"

Here is how you add a new command in 3 steps:

#### Step 1: Find or create the right cog in `cogs/`
Say you want to add a `/compliment` command. Since it's for fun, open [`cogs/fun.py`](../cogs/fun.py).

#### Step 2: Add your slash command
In `discord.py`, slash commands inside a cog look like this:

```python
@app_commands.command(name="compliment", description="Send a nice compliment to someone!")
@app_commands.describe(member="The person you want to compliment")
async def compliment(self, interaction: discord.Interaction, member: discord.Member):
    compliments = [
        "You light up every Discord channel you join!",
        "Your code is cleaner than a freshly formatted SSD!",
        "You're awesome!",
    ]
    chosen = random.choice(compliments)
    await interaction.response.send_message(f"💖 {member.mention}, {chosen}")
```

#### Step 3: Test and run!
Run the bot locally to try it out:
```bash
python bot.py
```
Or run pytest on the fun tests:
```bash
.venv/bin/pytest tests/test_flavor.py
```

---

## 💾 How Does the Database Work?

- The bot uses **SQLite** stored in `bot.db`.
- **Golden Rule**: Never write raw SQL strings across different files. Instead, all database interactions live in [`utils/db.py`](../utils/db.py).
- To fetch or save data, call helper functions in `utils/db.py`:
  ```python
  from utils import db

  # Example: check or update a balance
  balance = db.get_balance(user_id=interaction.user.id)
  db.add_coins(user_id=interaction.user.id, amount=50)
  ```

---

## 🧼 Housekeeping & Cleanliness

To keep the repository clean and easy to navigate:
1. **Never commit `.env` or `.log` files**: These are already in `.gitignore`.
2. **Build artifacts** (`_site/`, `_hub/`, `_owner/`, `_preview/`) are generated when building docs or previews; they are ignored by git and can be regenerated at any time via `scripts/`.
3. **Archived code** lives in [`deleted_features/`](../deleted_features/); if a feature is ever retired, it goes there rather than cluttering `cogs/`.
