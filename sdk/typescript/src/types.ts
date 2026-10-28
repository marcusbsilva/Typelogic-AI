/** Wire types shared by the local HTTP API and the Rust core contract. */
export type QuestionType = 'choice' | 'noul' | 'score';
export type ChoiceCriteria = Record<string, string | null>;

export interface QuestionSpec {
  type: QuestionType;
  instructions: string;
  criteria?: string[] | ChoiceCriteria;
  abstain_option?: string;
}

export interface DecisionRequest {
  state: unknown;
  questions: Record<string, QuestionSpec>;
  /** Optional; without it a short title is generated from the state after the evaluation. */
  title?: string;
  /** Up to 8 tags (lowercase after normalization); see GET /api/tags for the suggested ones. */
  tags?: string[];
  space?: string;
  threshold?: number;
  permutation_check?: boolean;
}

export type EvidenceValue = string | number | boolean | null;

/** Case / Evidence / Decision request: one case text, typed evidence and Jev-style decisions (answered as decision_1, ...). */
export interface CaseRequest {
  case: string;
  evidence?: Record<string, EvidenceValue | { value: EvidenceValue; source?: 'user' | 'extracted' | 'template'; status?: 'confirmed' | 'unverified' }>;
  evidence_schema?: { name: string; type: 'boolean' | 'number' | 'integer' | 'date' | 'text' | 'enum'; description?: string; options?: string[]; unit?: string; required?: boolean }[];
  attachments?: { type?: 'document' | 'text'; title?: string; content: string }[];
  decisions: { output: 'boolean' | 'choice' | 'score'; instructions: string; criteria?: string[] | ChoiceCriteria; abstain_option?: string; name?: string }[];
  /** Optional; without it a short title is generated from the case after the evaluation. */
  title?: string;
  /** Up to 8 tags (lowercase after normalization); see GET /api/tags for the suggested ones. */
  tags?: string[];
  space?: string;
  threshold?: number;
  permutation_check?: boolean;
}

export interface ValidatedQuestion {
  id: string; type: QuestionType; instr: string; keys: string[]; options: string[];
  criteria: unknown; abstain_option: string | null;
}

export interface Validated { state: string; questions: ValidatedQuestion[]; threshold: number; }

export interface Answer {
  type: QuestionType;
  instructions: string;
  criteria: unknown;
  probabilities: Record<string, number>;
  concentration: number;
  concentration_is_correctness_probability: false;
  calibration: unknown;
  abstained: boolean;
  requires_review: true;
  eligible_for_automation: false;
  review_reasons: string[];
  recommendation: 'candidate' | 'review';
  choice?: string;
  noul?: number;
  score?: number;
  legend?: Record<string, string>;
  order_diagnostic?: { checked: boolean; unstable?: boolean; max_probability_shift?: number };
}

export interface Integrity {
  format: string; sha512: string | null; md5: string | null; sha512_matches: boolean; md5_matches: boolean;
  verified: boolean; baseline_origin: string | null; scope: string;
}

export interface DecisionResponse {
  answers: Record<string, Answer>;
  decision_id: string;
  title: string;
  tags: string[];
  /** 'user', 'model:<ollama model>' or 'case text'. */
  title_source: string;
  created: string;
  integrity: Integrity;
  latency_ms: number;
  external_action_executed: false;
  [key: string]: unknown;
}

export interface StoredDecision {
  id: string; space: string; title: string; created: string;
  request: DecisionRequest; response: DecisionResponse; integrity: Integrity;
}

export interface LiteDecision { title: string; tags: string[]; questions: { question: string; answer: string | boolean }[]; }

/** Library limit and the next archive (GET /api/archive). */
export interface ArchiveInfo { count: number; limit: number; full: boolean; format: 'rar' | 'zip'; folder: string; name: string; note: string | null; }

export interface RouterRequest { task: string; language?: string; latency?: 'low' | 'medium' | 'high'; text?: string; }
export interface RouterProfile { name: string; tasks: string[]; languages: string[]; latency?: 'low' | 'medium' | 'high'; available?: boolean; priority?: number; }
export interface RouteResult {
  model: string | null; task: string; language: string; script: string | null; latency: string;
  candidates: string[]; skipped: { model: string; reason: string }[];
}
