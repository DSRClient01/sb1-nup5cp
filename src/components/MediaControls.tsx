import React from 'react';
import { Play, Pause, SkipForward, SkipBack, Volume2, VolumeX } from 'lucide-react';

interface MediaControlsProps {
  isPlaying: boolean;
  isMuted: boolean;
  volume: number;
  duration: number;
  currentTime: number;
  onPlayPause: () => void;
  onNext: () => void;
  onPrevious: () => void;
  onMute: () => void;
  onVolumeChange: (value: number) => void;
  onSeek: (value: number) => void;
  hasNext: boolean;
  hasPrevious: boolean;
}

export default function MediaControls({
  isPlaying,
  isMuted,
  volume,
  duration,
  currentTime,
  onPlayPause,
  onNext,
  onPrevious,
  onMute,
  onVolumeChange,
  onSeek,
  hasNext,
  hasPrevious,
}: MediaControlsProps) {
  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  return (
    <div className="px-4 py-3 bg-gray-50 rounded-lg space-y-2">
      {/* Progress Bar */}
      <div className="flex items-center gap-2">
        <span className="text-sm text-gray-600">{formatTime(currentTime)}</span>
        <input
          type="range"
          min={0}
          max={duration}
          value={currentTime}
          onChange={(e) => onSeek(Number(e.target.value))}
          className="flex-1 h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-blue-600"
        />
        <span className="text-sm text-gray-600">{formatTime(duration)}</span>
      </div>

      {/* Controls */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <button
            onClick={onPrevious}
            className="p-2 hover:bg-gray-200 rounded-full transition-colors disabled:opacity-50"
            disabled={!hasPrevious}
          >
            <SkipBack className="w-6 h-6" />
          </button>
          <button
            onClick={onPlayPause}
            className="p-3 bg-blue-600 hover:bg-blue-700 text-white rounded-full transition-colors"
          >
            {isPlaying ? <Pause className="w-6 h-6" /> : <Play className="w-6 h-6" />}
          </button>
          <button
            onClick={onNext}
            className="p-2 hover:bg-gray-200 rounded-full transition-colors disabled:opacity-50"
            disabled={!hasNext}
          >
            <SkipForward className="w-6 h-6" />
          </button>
        </div>

        {/* Volume Control */}
        <div className="flex items-center gap-2">
          <button
            onClick={onMute}
            className="p-2 hover:bg-gray-200 rounded-full transition-colors"
          >
            {isMuted ? <VolumeX className="w-6 h-6" /> : <Volume2 className="w-6 h-6" />}
          </button>
          <input
            type="range"
            min={0}
            max={1}
            step={0.1}
            value={isMuted ? 0 : volume}
            onChange={(e) => onVolumeChange(Number(e.target.value))}
            className="w-24 h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-blue-600"
          />
        </div>
      </div>
    </div>
  );
}