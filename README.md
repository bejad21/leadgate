# LeadGate

A comprehensive lead management and CRM automation platform combining Odoo, LLM-powered lead scoring, and multi-channel bot integration.

## Overview

LeadGate integrates:
- **Odoo 18** - Open-source ERP/CRM platform
- **LLM Integration** - Automated lead qualification using OpenRouter or Mistral APIs
- **Telegram Bot** - Real-time lead notifications and management
- **Supabase** - Scalable PostgreSQL backend
- **MongoDB** - Document storage for lead data and interactions

## Quick Start

### Prerequisites
- Docker and Docker Compose
- Git

### Setup

1. Clone the repository
2. Copy `.env.example` to `.env` and configure your API keys and database credentials
3. Start the services:
   ```bash
   docker compose up -d
   ```
4. Access Odoo at `http://localhost:8069`

## Project Structure

```
LeadGate/
├── docker-compose.yml      # Service orchestration
├── .env.example            # Environment variables template
├── .gitignore              # Git ignore rules
└── README.md               # This file
```

## Environment Variables

See `.env.example` for all required environment variables including:
- Odoo configuration
- LLM API keys (OpenRouter/Mistral)
- Telegram bot token
- Supabase credentials
- MongoDB connection string

## Development

(More details to follow in later tasks)

## License

(To be defined)
