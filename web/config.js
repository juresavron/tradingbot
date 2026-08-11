// Dashboard configuration. Both values are PUBLIC by design:
// the anon/publishable key only grants what row-level security allows,
// and all writes require a per-run secret key that never appears here.
window.TRADINGBOT_CONFIG = {
  SUPABASE_URL: "YOUR_SUPABASE_PROJECT_URL",       // e.g. https://abcdefgh.supabase.co
  SUPABASE_ANON_KEY: "YOUR_SUPABASE_ANON_KEY",     // Project settings -> API keys
};
