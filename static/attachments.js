/* Describe validated attachment delivery and processing notes without exposing file contents or inferring model support. */

export function attachmentIcon(path, kind = '') {
  const extension = path.split('.').pop().toLowerCase();
  if (kind === 'image' || ['png', 'jpg', 'jpeg', 'webp', 'gif'].includes(extension)) return 'image';
  if (kind === 'xlsx' || extension === 'xlsx') return 'sheet';
  if (kind === 'pptx' || extension === 'pptx') return 'presentation';
  return 'file-text';
}

export function attachmentSummary(file) {
  const labels = { image: 'Image', pdf: 'PDF', docx: 'DOCX', xlsx: 'XLSX', pptx: 'PPTX', text: 'UTF-8 text' };
  const delivery = { native: 'Original model input', extracted_text: 'Extracted text only', text: 'Text included' };
  const parts = [labels[file.kind] || 'File', delivery[file.delivery] || ''];
  if (Number.isSafeInteger(file.size_bytes) && file.size_bytes >= 0) parts.push(`${file.size_bytes.toLocaleString()} bytes`);
  if (Number.isSafeInteger(file.width) && Number.isSafeInteger(file.height)) parts.push(`${file.width.toLocaleString()} × ${file.height.toLocaleString()} pixels`);
  if (Number.isSafeInteger(file.pages)) parts.push(`${file.pages.toLocaleString()} page${file.pages === 1 ? '' : 's'}`);
  return parts.filter(Boolean).join(' · ');
}

export function attachmentWarnings(file) {
  return Array.isArray(file.warnings) ? file.warnings.filter(value => typeof value === 'string' && value.trim()).map(value => value.trim()) : [];
}

export function describeAttachmentNotes(files) {
  const details = files.flatMap(file => attachmentWarnings(file).map(warning => `${file.path}: ${warning}`));
  return details.length ? {
    title: 'Attachment processing notes',
    message: 'Review how the attached files were prepared for this request.',
    details,
  } : null;
}
