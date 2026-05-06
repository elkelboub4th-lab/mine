"use client";

import { SlidersHorizontal, Check } from "lucide-react";

interface AdminPanelProps {
  categories: string[];
  selectedCategory: string | null;
  onSelectCategory: (cat: string | null) => void;
  showStealsOnly: boolean;
  onToggleStealsOnly: () => void;
}

export function AdminPanel({
  categories,
  selectedCategory,
  onSelectCategory,
  showStealsOnly,
  onToggleStealsOnly,
}: AdminPanelProps) {
  return (
    <div className="glass rounded-2xl p-6 mb-8">
      <div className="flex items-center gap-3 mb-6">
        <div className="p-2 bg-emerald-500/10 rounded-xl">
          <SlidersHorizontal className="w-5 h-5 text-emerald-400" />
        </div>
        <h2 className="text-xl font-semibold text-slate-100">Sniper Settings</h2>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
        <div>
          <h3 className="text-sm font-medium text-slate-400 mb-3 uppercase tracking-wider">Models</h3>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => onSelectCategory(null)}
              className={`px-4 py-2 rounded-xl text-sm font-medium transition-all ${
                selectedCategory === null
                  ? 'bg-emerald-500/20 text-emerald-300 ring-1 ring-emerald-500/50'
                  : 'bg-white/5 text-slate-300 hover:bg-white/10'
              }`}
            >
              All Models
            </button>
            {categories.map(cat => (
              <button
                key={cat}
                onClick={() => onSelectCategory(cat)}
                className={`px-4 py-2 rounded-xl text-sm font-medium transition-all ${
                  selectedCategory === cat
                    ? 'bg-emerald-500/20 text-emerald-300 ring-1 ring-emerald-500/50'
                    : 'bg-white/5 text-slate-300 hover:bg-white/10'
                }`}
              >
                {cat}
              </button>
            ))}
          </div>
        </div>

        <div>
          <h3 className="text-sm font-medium text-slate-400 mb-3 uppercase tracking-wider">Filters</h3>
          <button
            onClick={onToggleStealsOnly}
            className={`flex items-center gap-3 px-4 py-3 rounded-xl w-full sm:w-auto transition-all ${
              showStealsOnly
                ? 'steal-gradient text-white shadow-lg shadow-emerald-500/20 ring-1 ring-emerald-400/50'
                : 'bg-white/5 text-slate-300 hover:bg-white/10 ring-1 ring-white/5'
            }`}
          >
            <div className={`w-5 h-5 rounded-full flex items-center justify-center ${showStealsOnly ? 'bg-white text-emerald-500' : 'bg-white/10'}`}>
              {showStealsOnly && <Check className="w-3 h-3" />}
            </div>
            Show Steals Only 🔥
          </button>
        </div>
      </div>
    </div>
  );
}
