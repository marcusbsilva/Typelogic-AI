// Wire types of the local API (server.py).

export type QuestionType = 'choice' | 'noul' | 'score';
export type ChoiceCriteria = Record<string, string | null>;

export interface QuestionSpec {
  type: QuestionType;
  instructions: string;
  criteria?: string[] | ChoiceCriteria;
  abstain_option?: string;
}

export interface OrderDiagnostic { checked: boolean; unstable?: boolean; }

export interface Answer {
  type: QuestionType;
  instructions?: string;
  criteria?: ChoiceCriteria | string[] | null;
  probabilities: Record<string, number>;
  concentration: number;
  abstained: boolean;
  review_reasons: string[];
  recommendation: 'candidate' | 'review';
  choice?: string;
  noul?: number;
  score?: number;
  legend?: Record<string, string>;
  order_diagnostic?: OrderDiagnostic;
  engine?: 'specialist' | 'generalist' | 'ensemble';
}

export interface Integrity { verified?: boolean; sha512?: string | null; md5?: string | null; baseline_origin?: string | null; }

export interface FieldSpec { name: string; type: 'text' | 'number' | 'integer' | 'date' | 'boolean'; description?: string; options?: string[]; }

export interface ExtractedField { name: string; type: string; value: string | number | boolean | null; check: string; }

export interface ExtractionResult {
  engine: 'needle3'; fields: ExtractedField[]; chars: number; seconds: number; confidence: number | null;
  withheld_by_engine: boolean; reasoning: string; note: string;
}

export interface ExtractedFields { engine: 'needle3'; fields: ExtractedField[]; note?: string; }

export interface NeedleStatus {
  available: boolean; reason: string | null; loaded: boolean;
  limits: { max_text_chars: number; max_fields: number };
  presets: Record<string, { label: string; fields: FieldSpec[] }>;
}

export type EvidenceType = 'boolean' | 'number' | 'integer' | 'date' | 'text' | 'enum';
export type EvidenceValue = string | number | boolean | null;
export interface EvidenceSpec { name: string; type: EvidenceType; description?: string; options?: string[]; unit?: string; required?: boolean; }
export interface EvidenceEntry { value: EvidenceValue; source: 'user' | 'extracted' | 'template'; status: 'confirmed' | 'unverified'; check?: string; }
/** One decision, Jev style: no name (answered as decision_1, ...), only the question and its output. */
export interface DecisionSpec {
  output: 'boolean' | 'choice' | 'score'; instructions: string; name?: string;
  criteria?: ChoiceCriteria | string[] | Record<string, string>; alternatives?: ChoiceCriteria; levels?: string[] | Record<string, string>;
  abstain_option?: string;
}
export interface CaseTemplate {
  id: string; title: string; description?: string; example?: boolean;
  case?: string; tags?: string[]; evidence_schema?: EvidenceSpec[]; evidence?: Record<string, EvidenceValue>;
  decisions: DecisionSpec[]; threshold?: number; permutation_check?: boolean;
}
export interface TemplateRow { id: string; title: string; description: string; origin: 'builtin' | 'project' | 'local'; template: CaseTemplate; }
export interface ExtractedEvidence { name: string; type: string; value: EvidenceValue; check: string; status: string; }
export interface EvidenceExtraction { backend: string; model?: string; fields: ExtractedEvidence[]; seconds: number; chars: number; note: string; }
/** Library limit and the next archive (GET /api/archive, typelogic/archive.py). */
export interface ArchiveInfo { count: number; limit: number; full: boolean; format: 'rar' | 'zip'; folder: string; name: string; note: string | null; }
export interface ArchiveResult { path: string; name: string; format: 'rar' | 'zip'; decisions: number; }
export interface TagInfo { suggested: string[]; max_tags: number; max_chars: number; }

export interface DecisionResponse {
  answers: Record<string, Answer>;
  decision_id: string;
  title?: string;
  tags?: string[];
  title_source?: string;
  created?: string;
  integrity?: Integrity;
  latency_ms: number;
  extracted_fields?: ExtractedFields;
  [key: string]: unknown;
}

export interface CaseRequest {
  space: string; template?: string; title?: string; tags?: string[]; case: string;
  evidence_schema: EvidenceSpec[]; evidence: Record<string, EvidenceEntry>;
  decisions: DecisionSpec[]; permutation_check: boolean;
}

export interface StoredDecision {
  id: string;
  title: string;
  space: string;
  created: string;
  request: { questions?: Record<string, QuestionSpec>; tags?: string[] } & Record<string, unknown>;
  response: DecisionResponse;
  integrity?: Integrity;
}

export interface TrainingExport {
  records: Record<string, unknown>[];
  skipped: { integrity_failed: number; uncorrected: number };
  corrections: number;
}

export interface CoreInfo { backend: 'rust' | 'python'; core_version?: string; reason?: string; }

export interface EngineHealth {
  status: 'not_loaded' | 'loading' | 'ready' | 'error' | string;
  error?: string | null;
  model?: string;
  base_model?: string;
  device?: string;
  max_state_tokens?: number;
  calibration?: string | { status?: string } | null;
  onnx?: { runtime?: string; providers?: string[] } | null;
  readout?: 'onnx' | 'rust' | 'torch';
  core?: CoreInfo;
  model_dir?: string;
  profile?: Record<string, QuestionSpec> | null;
  generalist?: GeneralistHealth | null;
  routing?: { max_generalist_options?: number; mode?: string; ensemble_weight?: number | null; forced?: string | null } | null;
  [key: string]: unknown;
}

export interface GeneralistHealth {
  status?: string;
  backend?: 'ollama' | 'torch' | 'gguf' | string;
  ollama_model?: string | null;
  gguf?: string | null;
  device?: string | null;
  option_orders?: number;
  calibration?: string | null;
}

export interface StatusResponse {
  engine: EngineHealth;
  mode?: string;
  restarting?: boolean;
  stats: { decisions: number; feedback: number };
  library_limit?: number;
}

export interface ExampleRow { id: string; title: string; description: string; payload: Record<string, unknown> & { questions: Record<string, QuestionSpec> }; }

export interface TypeLogicThemeApi { get(): string; set(value: string): void; }

declare global {
  interface Window { TypeLogicTheme: TypeLogicThemeApi; }
}


export interface ModeOption { available: boolean; detail?: string | null; reason?: string | null; }
export interface ModeInfo {
  mode: string;
  label: string;
  restarting: boolean;
  engine_status?: string;
  settings: { hybrid_device: 'cpu' | 'gpu'; hybrid_combine: 'route' | 'ensemble'; ollama_model: string };
  availability: {
    cpu: ModeOption;
    gpu: ModeOption & { backend?: string | null; name?: string | null; via_restart?: boolean };
    hybrid: ModeOption & { gpu_device?: boolean; models?: string[]; ollama_model?: string };
  };
}
