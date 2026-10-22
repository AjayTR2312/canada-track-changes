import axios from 'axios';

const API_URL = import.meta.env.VITE_API_URL || '';

const api = axios.create({
  baseURL: API_URL,
  timeout: 300000, // 5 min for long operations
});

// ─── Types ───
export interface JobStatus {
  job_id: string;
  status: 'running' | 'completed' | 'failed' | 'stopped';
  current_step: number;
  current_step_name: string;
  total_steps: number;
  progress_pct: number;
  file_name: string;
  output_dir: string;
  output_file: string;
  error: string;
  error_info?: ErrorInfo | null;
  validation_error?: string;
  validation_error_info?: ErrorInfo | null;
  log: string;
  product_type: string;
  user_email: string;
  started_at: number;
  completed_at: number | null;
  llm_calls?: number;
}

export interface UserProfile {
  emp_id: string;
  name: string;
  email: string;
  dept: string;
  title: string;
  region?: string;
  _local_only?: boolean;
}

export interface UsageLog {
  date: string;
  time: string;
  emp_id: string;
  name: string;
  service_type: string;
  region: string;
  product: string;
  files_uploaded: number;
  inserts_count: number;
  inserts_completed: number;
  llm_calls: number;
  files_completed: number;
  files_failed: number;
  run_outcome: string;
  processing_time_sec: number;
  run_number_today: number;
}

// ─── API Calls ───

export async function healthCheck() {
  const res = await api.get('/api/health');
  return res.data;
}

export async function startProcessing(
  docxFile: File,
  pxmlFile: File,
  productType: string,
  userEmail: string,
  empId: string,
  imagesZip?: File | null,
  processingMode: string = 'chapter'
) {
  const formData = new FormData();
  formData.append('docx_file', docxFile);
  formData.append('pxml_file', pxmlFile);
  formData.append('product_type', productType);
  formData.append('processing_mode', processingMode);
  formData.append('user_email', userEmail);
  formData.append('emp_id', empId);
  if (imagesZip) {
    formData.append('images_zip', imagesZip);
  }
  const res = await api.post('/api/process', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return res.data as { job_id: string; status: string };
}

export async function getJobStatus(jobId: string): Promise<JobStatus> {
  const res = await api.get(`/api/status/${jobId}`);
  return res.data;
}

export async function getJobLog(jobId: string): Promise<string> {
  const res = await api.get(`/api/log/${jobId}`);
  return res.data.log || '';
}

export async function cancelJob(jobId: string): Promise<{ status: string; job_id: string }> {
  const res = await api.post(`/api/cancel/${jobId}`);
  return res.data;
}

// ─── Batch processing ───
// A publication's worth of chapters, submitted together. Chapters run one at
// a time through the SAME /api/process path startProcessing above uses —
// batch only adds grouping + a queue, never a second processing pipeline.

export interface BatchGroup {
  task_name: string;
  docx: string | null;
  xml: string | null;
  zip: string | null;
  missing: string[];
  extras: string[];
  confidence: number;
  low_confidence: boolean;
  excluded: boolean;
}

/** A failure explained for the user (built by usforms_pipeline/error_info.py). */
export interface ErrorInfo {
  category: 'llm_busy' | 'llm_auth' | 'llm_request' | 'llm_config' | 'stopped' | 'input' | 'storage' | 'content' | 'unknown';
  title: string;
  hint: string;
  /** What to do first: run again, run again then report, fix the input files, work manually, or report to the owner. */
  action: 'rerun' | 'rerun_then_report' | 'fix_input' | 'manual' | 'report';
  retryable: boolean;
  detail: string;
}

export interface BatchItem extends BatchGroup {
  status: 'queued' | 'running' | 'completed' | 'failed' | 'skipped';
  job_id: string;
  error: string;
  error_info?: ErrorInfo | null;
  validation_error?: string;
  validation_error_info?: ErrorInfo | null;
  /** True while a validation-only re-run is in progress for this finished chapter. */
  revalidating?: boolean;
  output_dir: string;
  current_step_name?: string;
  progress_pct?: number;
  current_step?: number;
  total_steps?: number;
  /** Files copied to the batch's S3 output folder (see s3_output_folder on the batch). */
  s3_published?: number;
  s3_error?: string;
  /** s3://bucket/folder/ the chapter's output was published to. */
  s3_location?: string;
}

export interface BatchPlanResponse {
  batch_id: string;
  groups: BatchGroup[];
  all_files: string[];
}

export interface BatchStatusResponse {
  batch_id: string;
  status: 'planned' | 'running' | 'completed' | 'cancelled';
  source: string;
  items: BatchItem[];
  completed: number;
  total: number;
  s3_output_folder?: string;
  groups?: BatchGroup[];
  all_files?: string[];
  log?: BatchLogEntry[];
}

export interface BatchLogEntry {
  time: string;
  level: 'info' | 'warning';
  message: string;
}

export async function planBatch(files: File[]): Promise<BatchPlanResponse> {
  const formData = new FormData();
  files.forEach((f) => formData.append('files', f));
  const res = await api.post('/api/batch/plan', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return res.data;
}

/** Plan a batch from a folder in the S3 bucket (e.g. "incoming/Publication_6121007" or an s3:// link). */
export async function planBatchFromS3(s3Folder: string): Promise<BatchPlanResponse> {
  const formData = new FormData();
  formData.append('s3_folder', s3Folder);
  const res = await api.post('/api/batch/plan-s3', formData);
  return res.data;
}

export async function startBatch(
  batchId: string,
  groups: BatchGroup[],
  productType: string,
  userEmail: string,
  empId: string,
  processingMode: string = 'chapter',
  publicationName: string = ''
): Promise<BatchStatusResponse> {
  const formData = new FormData();
  formData.append('groups', JSON.stringify(groups));
  formData.append('product_type', productType);
  formData.append('processing_mode', processingMode);
  formData.append('user_email', userEmail);
  formData.append('emp_id', empId);
  formData.append('publication_name', publicationName);
  const res = await api.post(`/api/batch/${batchId}/start`, formData);
  return res.data;
}

export async function getBatchStatus(batchId: string): Promise<BatchStatusResponse> {
  const res = await api.get(`/api/batch/${batchId}`);
  const data = res.data;
  if (!data || typeof data !== 'object' || typeof data.status !== 'string') {
    console.error('[getBatchStatus] unexpected response body:', typeof data, String(data).slice(0, 300));
    throw new Error('Unexpected response from /api/batch — check that the backend is running and up to date.');
  }
  return data;
}

export async function cancelBatch(batchId: string): Promise<{ batch_id: string; status: string }> {
  const res = await api.post(`/api/batch/${batchId}/cancel`);
  return res.data;
}

export async function retryBatchChapter(
  batchId: string,
  taskName: string
): Promise<{ batch_id: string; task_name: string; status: string }> {
  const res = await api.post(`/api/batch/${batchId}/retry/${encodeURIComponent(taskName)}`);
  return res.data;
}

/** Run a failed / stopped job again from the input files still saved on the server. Returns the new job id. */
export async function rerunJob(jobId: string): Promise<{ job_id: string; rerun_of: string }> {
  const res = await api.post(`/api/job/${jobId}/rerun`);
  return res.data;
}

/** Re-run ONLY the validation of a job whose conversion already finished (skips the costly conversion). */
export async function revalidateJob(jobId: string): Promise<{ status: string; job_id: string }> {
  const res = await api.post(`/api/job/${jobId}/revalidate`);
  return res.data;
}

export async function retryBatchPush(
  batchId: string,
  taskName: string
): Promise<{ task_name: string; s3_published: number; s3_error: string; s3_location: string }> {
  const res = await api.post(`/api/batch/${batchId}/retry-push/${encodeURIComponent(taskName)}`);
  return res.data;
}

export async function downloadOutput(jobId: string): Promise<Blob> {
  const res = await api.get(`/api/download/${jobId}`, { responseType: 'blob' });
  return res.data;
}

export async function downloadValidation(jobId: string): Promise<Blob> {
  const res = await api.get(`/api/download-validation/${jobId}`, { responseType: 'blob' });
  return res.data;
}

export async function listOutputs(jobId: string): Promise<string[]> {
  const res = await api.get(`/api/list-outputs/${jobId}`);
  const files = res.data.files || [];
  // The backend returns objects { key, filename, size }. Normalize to filename
  // strings (also tolerating a plain-string shape) so callers can safely treat
  // each entry as a path string.
  return files
    .map((f: any) => (typeof f === 'string' ? f : (f?.filename || f?.key || '')))
    .filter(Boolean);
}

export async function sendCompletionEmail(jobId: string, toEmail: string) {
  const formData = new FormData();
  formData.append('to_email', toEmail);
  const res = await api.post(`/api/send-completion-email/${jobId}`, formData);
  return res.data;
}

// ─── Auth/User APIs (local JSON-based, wrapped via backend or direct) ───
// These hit a lightweight auth API we add to the FastAPI backend

export async function loginUser(empId: string): Promise<UserProfile | null> {
  try {
    const res = await api.post('/api/auth/login', { emp_id: empId });
    return res.data;
  } catch {
    return null;
  }
}

export async function registerUser(profile: {
  emp_id: string;
  name: string;
  email: string;
  dept: string;
}): Promise<UserProfile> {
  const res = await api.post('/api/auth/register', profile);
  return res.data;
}

export async function updateUserProfile(profile: {
  emp_id: string;
  name: string;
  email: string;
  dept: string;
}): Promise<UserProfile> {
  const res = await api.put('/api/auth/update', profile);
  return res.data;
}

export async function getUsageLogs(empId?: string): Promise<UsageLog[]> {
  const params = empId ? { emp_id: empId } : {};
  const res = await api.get('/api/auth/usage-logs', { params });
  return res.data;
}

export interface RecentJob {
  date: string;
  time: string;
  emp_id: string;
  name: string;
  service_type: string;
  region: string;
  product: string;
  files_uploaded: number;
  files_completed: number;
  files_failed: number;
  run_outcome: string;
  processing_time_sec: number;
  session_id: string;
  file_name?: string;
  download_available: boolean;
  validation_done?: boolean;
  validation_resumable?: boolean;
}

export async function getRecentJobs(empId: string): Promise<RecentJob[]> {
  const res = await api.get('/api/auth/recent-jobs', { params: { emp_id: empId } });
  const data = res.data;
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.jobs)) return data.jobs;
  // Anything else (typically an HTML page or text body from a proxy/stale
  // server) used to be stored as-is and crash the whole tab at jobs.map().
  console.error('[getRecentJobs] unexpected response body:', typeof data, String(data).slice(0, 300));
  throw new Error('Unexpected response from /api/auth/recent-jobs (not a job list) — check that the backend is running and up to date.');
}

export async function downloadBySession(sessionId: string): Promise<Blob> {
  const res = await api.get(`/api/download-by-session/${sessionId}`, { responseType: 'blob' });
  return res.data;
}

// Build a validation session directly from a finished pipeline job's on-disk files
// (DOCX + final PXML + PDF + Failed_revisions.xlsx). Idempotent on the backend.
export async function createSessionFromJob(jobId: string, empId: string): Promise<{ session_id: string } & Record<string, any>> {
  const res = await api.post('/api/validation/create-session-from-job', { job_id: jobId, emp_id: empId });
  return res.data;
}

/** Resume a validation session. Its files live in S3, so this works from any server instance. */
export async function resumeValidationSession(sessionId: string, empId: string): Promise<any> {
  const formData = new FormData();
  formData.append('session_id', sessionId);
  formData.append('emp_id', empId);
  const res = await api.post('/api/validation/resume', formData);
  return res.data;
}

export async function deleteValidationSession(sessionId: string, empId: string): Promise<any> {
  const res = await api.delete(`/api/validation/session/${sessionId}`, { params: { emp_id: empId } });
  return res.data;
}

export default api;
