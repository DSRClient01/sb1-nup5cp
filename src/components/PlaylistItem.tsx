import React from 'react';

interface PlaylistItemProps {
  name: string;
  type: 'audio' | 'video';
  isActive: boolean;
  onClick: () => void;
}

export default function PlaylistItem({ name, type, isActive, onClick }: PlaylistItemProps) {
  return (
    <div
      onClick={onClick}
      className={`p-3 rounded-lg cursor-pointer flex items-center gap-3 ${
        isActive ? 'bg-blue-100 text-blue-700' : 'hover:bg-gray-100'
      }`}
    >
      {type === 'video' ? '🎥' : '🎵'}
      <span className="flex-1 truncate">{name}</span>
    </div>
  );
}