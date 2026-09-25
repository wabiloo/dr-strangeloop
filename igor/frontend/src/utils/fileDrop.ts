import { uploadAsset } from '../api/client'

interface FileWithNativePath extends File {
  /** Electron and a few desktop browser shells expose the original path. */
  path?: string
}

/**
 * Return a path that the backend can use for a dropped file. Ordinary
 * browsers only expose the file bytes, so upload those bytes to Igor; desktop
 * shells that expose an absolute path can avoid the copy.
 */
export async function sourceFromDroppedFile(file: File): Promise<string> {
  const nativePath = (file as FileWithNativePath).path?.trim()
  if (nativePath) return nativePath
  return (await uploadAsset(file)).path
}

export function filesFromDrop(event: DragEvent): File[] {
  return event.dataTransfer ? Array.from(event.dataTransfer.files) : []
}

export function isFileDrag(event: DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes('Files')
}

export function isDragInside(event: DragEvent): boolean {
  return event.currentTarget instanceof Node && event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget)
}
