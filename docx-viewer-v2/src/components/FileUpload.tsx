import React, { useRef } from 'react';
import './FileUpload.css';

interface Props {
  onFileSelect: (file: File) => void;
  isLoading: boolean;
  fileName: string | null;
}

const FileUpload: React.FC<Props> = ({ onFileSelect, isLoading, fileName }) => {
  const inputRef = useRef<HTMLInputElement>(null);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      onFileSelect(file);
    }
  };

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    const file = e.dataTransfer.files?.[0];
    if (file && file.name.endsWith('.docx')) {
      onFileSelect(file);
    }
  };

  const handleDragOver = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
  };

  return (
    <div className="file-upload-bar">
      <div
        className="upload-area"
        onClick={() => inputRef.current?.click()}
        onDrop={handleDrop}
        onDragOver={handleDragOver}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".docx"
          onChange={handleChange}
          hidden
        />
        {isLoading ? (
          <span className="upload-loading">⏳ Parsing document...</span>
        ) : fileName ? (
          <span className="upload-filename">📄 {fileName} – click or drop to change</span>
        ) : (
          <span className="upload-prompt">📂 Click or drag & drop a .docx file here</span>
        )}
      </div>
    </div>
  );
};

export default FileUpload;
