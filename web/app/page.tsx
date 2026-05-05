"use client";

import { useEffect, useState, useMemo } from "react";
import { createClient } from "@supabase/supabase-js";
import { ListingCard } from "../components/ListingCard";
import { AdminPanel } from "../components/AdminPanel";
import { Target, Activity } from "lucide-react";

// Initialize Supabase client
const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || "https://placeholder.supabase.co";
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "placeholder";
const supabase = createClient(supabaseUrl, supabaseAnonKey);

export default function Dashboard() {
  const [listings, setListings] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [showStealsOnly, setShowStealsOnly] = useState(false);

  useEffect(() => {
    // Initial fetch
    fetchListings();

    // Set up real-time subscription
    const channel = supabase
      .channel('public:listings')
      .on(
        'postgres_changes',
        { event: 'INSERT', schema: 'public', table: 'listings' },
        (payload) => {
          setListings((current) => [payload.new, ...current]);
        }
      )
      .subscribe();

    return () => {
      supabase.removeChannel(channel);
    };
  }, []);

  const fetchListings = async () => {
    try {
      const { data, error } = await supabase
        .from('listings')
        .select('*')
        .order('created_at', { ascending: false })
        .limit(100);

      if (error) throw error;
      if (data) setListings(data);
    } catch (err) {
      console.error("Error fetching listings:", err);
    } finally {
      setLoading(false);
    }
  };

  const categories = useMemo(() => {
    const cats = new Set(listings.map(l => l.metadata?.model || l.category).filter(Boolean));
    return Array.from(cats).sort();
  }, [listings]);

  const filteredListings = useMemo(() => {
    return listings.filter(l => {
      const model = l.metadata?.model || l.category;
      if (selectedCategory && model !== selectedCategory) return false;
      if (showStealsOnly && !l.is_steal) return false;
      return true;
    });
  }, [listings, selectedCategory, showStealsOnly]);

  return (
    <div>
      <header className="flex items-center justify-between mb-10 pb-6 border-b border-white/10">
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 rounded-2xl steal-gradient flex items-center justify-center shadow-[0_0_20px_rgba(16,185,129,0.3)]">
            <Target className="w-6 h-6 text-white" />
          </div>
          <div>
            <h1 className="text-3xl font-bold tracking-tight text-white">SwoopDZ</h1>
            <p className="text-slate-400 text-sm mt-1">Algerian Deal Sniper</p>
          </div>
        </div>
        <div className="flex items-center gap-2 px-4 py-2 rounded-full glass text-sm font-medium">
          <span className="relative flex h-3 w-3">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-3 w-3 bg-emerald-500"></span>
          </span>
          Live Feed
        </div>
      </header>

      <AdminPanel 
        categories={categories}
        selectedCategory={selectedCategory}
        onSelectCategory={setSelectedCategory}
        showStealsOnly={showStealsOnly}
        onToggleStealsOnly={() => setShowStealsOnly(!showStealsOnly)}
      />

      <div className="mb-6 flex items-center justify-between">
        <h2 className="text-xl font-semibold flex items-center gap-2">
          <Activity className="w-5 h-5 text-emerald-400" />
          Market Activity
          <span className="text-sm font-normal text-slate-500 ml-2">
            ({filteredListings.length} {filteredListings.length === 1 ? 'iPhone' : 'iPhones'})
          </span>
        </h2>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-20">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-emerald-500"></div>
        </div>
      ) : filteredListings.length === 0 ? (
        <div className="glass rounded-2xl p-12 text-center border-dashed border-2 border-white/10">
          <p className="text-slate-400 text-lg">No listings found matching your criteria.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {filteredListings.map(listing => (
            <ListingCard key={listing.id} listing={listing} />
          ))}
        </div>
      )}
    </div>
  );
}
