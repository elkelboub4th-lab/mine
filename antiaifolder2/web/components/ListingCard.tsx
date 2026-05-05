import { ExternalLink, Tag, TrendingDown, Clock } from "lucide-react";

interface Listing {
  id: string;
  title: string;
  price: number;
  url: string;
  category: string;
  is_steal: boolean;
  created_at: string;
  metadata: {
    estimated_market_price_dzd?: number;
    is_fake_price?: boolean;
    model?: string;
  };
}

export function ListingCard({ listing }: { listing: Listing }) {
  const isSteal = listing.is_steal;
  const timeAgo = new Date(listing.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  return (
    <div className={`glass rounded-2xl p-5 transition-all duration-300 hover:-translate-y-1 hover:shadow-xl ${isSteal ? 'ring-2 ring-emerald-500/50 shadow-emerald-500/10' : 'border border-white/5'}`}>
      <div className="flex justify-between items-start mb-4">
        <div className="flex gap-2 items-center">
          <span className="text-xs font-medium px-2.5 py-1 rounded-full bg-white/5 text-slate-300 border border-white/10 flex items-center gap-1.5">
            <Tag className="w-3 h-3" />
            {listing.metadata?.model || listing.category || "General"}
          </span>
          {isSteal && (
            <span className="text-xs font-bold px-2.5 py-1 rounded-full steal-gradient text-white shadow-[0_0_15px_rgba(16,185,129,0.3)] animate-pulse">
              🔥 STEAL
            </span>
          )}
        </div>
        <span className="text-xs text-slate-500 flex items-center gap-1">
          <Clock className="w-3 h-3" />
          {timeAgo}
        </span>
      </div>

      <h3 className="font-semibold text-lg mb-2 text-slate-100 line-clamp-2 leading-tight">
        {listing.title}
      </h3>

      <div className="mt-4 flex items-end justify-between">
        <div>
          <div className="text-2xl font-bold text-emerald-400">
            {listing.price.toLocaleString()} <span className="text-sm font-normal text-emerald-500/70">DZD</span>
          </div>
          {listing.metadata?.estimated_market_price_dzd && (
            <div className="text-sm text-slate-500 flex items-center gap-1 mt-1">
              <TrendingDown className="w-3.5 h-3.5" />
              Est. Market: {listing.metadata.estimated_market_price_dzd.toLocaleString()}
            </div>
          )}
        </div>
        
        <a 
          href={listing.url} 
          target="_blank" 
          rel="noopener noreferrer"
          className="flex items-center gap-2 bg-white/5 hover:bg-white/10 transition-colors px-4 py-2 rounded-xl text-sm font-medium text-slate-200"
        >
          View
          <ExternalLink className="w-4 h-4" />
        </a>
      </div>
    </div>
  );
}
