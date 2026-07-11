// 持仓相关类型
export interface Position {
  account: string
  code: string
  name: string
  currency: 'CNY' | 'USD' | 'HKD' | string
  asset_type: 'stock' | 'custom' | string
  quantity: number
  available_qty: number
  cost_price: number
  total_cost?: number
  fee?: number | null
  note?: string
  source?: string
  current_price: number
  profit: number
  profit_cny?: number | null
  profit_pct: number
  market_value: number
  market_value_cny?: number | null
  fx_rate?: number | null
  asset_weight_pct?: number
  holding_weight_pct?: number
}

export interface CashAccount {
  account: string
  currency: string
  amount: number
  fx_rate?: number | null
  amount_cny?: number | null
  asset_weight_pct?: number
  updated_at?: string
}

export interface Portfolio {
  positions: Position[]
  cash_accounts: CashAccount[]
  cash: number
  legacy_cash_cny?: number
  base_currency: string
  fx_rates: Record<string, number>
  fx_updated_at?: string | null
  currency_totals?: Record<string, {
    market_value: number
    cash: number
    profit: number
    market_value_cny: number
    cash_cny: number
    profit_cny: number
  }>
  total_market_value: number
  total_assets: number
  total_profit: number
}

export interface PortfolioSummary {
  total_positions: number
  total_assets: number
  cash: number
  market_value: number
  total_profit: number
  profit_pct: number
  winners_count: number
  losers_count: number
  top_winner: Position | null
  top_loser: Position | null
}

// 行情相关类型
export interface Quote {
  symbol: string
  name: string
  price: number
  open: number
  high: number
  low: number
  prev_close: number
  volume: number
  change: number
  change_pct: number
  amount?: number        // 成交额
  turnover?: number      // 换手率
  pe?: number           // 市盈率
  timestamp?: string    // 更新时间
}

// 对话相关类型
export interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp?: string
  image?: string  // 为了向后兼容旧的单张图片数据
  images?: string[]  // 支持多张图片
  pendingAction?: PendingAction
}

export interface ChatResponse {
  response: string
  suggestions: Suggestion[]
  risks: Risk[]
  sentiment: 'bullish' | 'bearish' | 'neutral'
  memory_updates: MemoryUpdate[]
  imported_positions?: number  // 导入的持仓数量
}

export interface PendingChange {
  action_type?: string
  account?: string
  name?: string
  code?: string | null
  currency?: string | null
  asset_type?: string | null
  quantity?: number | null
  cost_price?: number | null
  total_cost?: number | null
  fee?: number | null
  note?: string
  source?: string
  amount?: number | null
}

export interface RevisionDiff {
  kind: 'reverted' | 'applied' | string
  change_index: number
  field: string
  label: string
  before: string | number | null
  after: string | number | null
  before_text: string
  after_text: string
}

export interface CorrectionContext {
  corrects_pending_id?: string
  restored_from_pending_id?: string
}

export interface RevisionOption {
  field: string
  value: string | number
  label: string
  message: string
}

export interface InstrumentCandidate {
  code: string
  name: string
  currency: string
  asset_type: string
  score?: number
  match_score?: number
  change_index?: number
}

export interface PendingItem {
  item_id: string
  index: number
  summary?: string
  action_type?: string
  changes: PendingChange[]
  missing_fields: string[]
  warnings: string[]
  instrument_candidates?: InstrumentCandidate[]
  revision_options?: RevisionOption[]
  revision_diffs?: RevisionDiff[]
  status?: string
  requires_confirmation: boolean
  operation_id?: string | null
  version?: number
}

export interface PendingAction {
  ok: boolean
  pending_id?: string
  summary: string
  action_type?: string
  changes: PendingChange[]
  items?: PendingItem[]
  missing_fields: string[]
  warnings: string[]
  requires_confirmation: boolean
  intent?: 'bookkeeping' | 'chat_only' | string
  status?: string
  operation_id?: string | null
  operation_ids?: string[]
  can_confirm_all?: boolean
  pending_item_count?: number
  confirmed_item_count?: number
  rolled_back_item_count?: number
  instrument_candidates?: InstrumentCandidate[]
  revision_options?: RevisionOption[]
  revision_diffs?: RevisionDiff[]
  correction_context?: CorrectionContext | null
  revises_pending_id?: string
  revised_to_pending_id?: string
}

export interface OperationSummary {
  operation_id: string
  created_at: string
  type: string
  summary: string
  imported_positions: number
  can_rollback: boolean
  status?: string
}

// 建议相关类型
export interface Suggestion {
  type: 'buy' | 'sell' | 'hold' | 'reduce' | 'add'
  symbol: string | null
  reason: string
  target_price: number | null
  stop_loss: number | null
  position_size: string | null
  confidence: 'high' | 'medium' | 'low'
}

export interface Risk {
  level: 'high' | 'medium' | 'low'
  type: string
  description: string
  suggestion: string
}

// 记忆相关类型
export interface UserProfile {
  name: string
  experience_level: 'beginner' | 'intermediate' | 'expert'
  trading_style: 'day' | 'swing' | 'position' | 'value'
  risk_tolerance: 'conservative' | 'moderate' | 'aggressive'
  notes: string
  preferred_sectors?: string[]
  typical_position_size?: string
  holding_period?: string
}

export interface TradingPreferences {
  preferred_sectors?: string[]
  avoid_sectors?: string[]
  max_single_position?: number
  preferred_holding_period?: string
  emotional_triggers?: string[]
  news_sensitivity?: string
  avoid_patterns?: string[]
  preferred_indicators?: string[]
}

export interface TradingLesson {
  date: string
  type: 'win' | 'loss' | 'mistake' | 'insight'
  symbol: string | null
  description: string
  lesson: string
}

export interface UserGoals {
  short_term: string[] | string
  long_term: string[] | string
  monthly_target?: number | null
  annual_target?: number | null
  learning?: string[]
}

export interface UserMemory {
  profile: UserProfile
  preferences: TradingPreferences
  history: {
    lessons: TradingLesson[]
  }
  goals: UserGoals
}

export interface MemoryUpdate {
  category: string
  content: string
  confidence: number
}

// API 响应类型
export interface ApiResponse<T> {
  data?: T
  error?: string
  message?: string
}
