-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Users table (minimal MVP: just telegram_id and preferences)
CREATE TABLE users (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  telegram_id TEXT UNIQUE NOT NULL,
  preferences JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Listings table for all valid scraped items
CREATE TABLE listings (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  external_id TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  price NUMERIC,
  url TEXT NOT NULL,
  category TEXT,
  status TEXT DEFAULT 'active',
  is_steal BOOLEAN DEFAULT false,
  metadata JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seen IDs for fast duplicate checking (to prevent re-scraping the same item and running OpenAI on it again)
CREATE TABLE seen_listings (
  external_id TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
