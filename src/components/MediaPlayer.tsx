import React, { useState, useRef, useEffect } from 'react';
import { Upload, Download } from 'lucide-react';
import JSZip from 'jszip';
import MediaControls from './MediaControls';
import PlaylistItem from './PlaylistItem';
import AudioVisualizer from './AudioVisualizer';

interface MediaItem {
  id: string;
  name: string;
  type: 'audio' | 'video';
  url: string;
  file?: File;
}

export default function MediaPlayer() {
  const [playlist, setPlaylist] = useState<MediaItem[]>([]);
  const [currentIndex, setCurrentIndex] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [isMuted, setIsMuted] = useState(false);
  const [volume, setVolume] = useState(1);
  const [duration, setDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [isExporting, setIsExporting] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const currentMedia = playlist[currentIndex];
  const isAudio = currentMedia?.type === 'audio';
  const mediaRef = isAudio ? audioRef : videoRef;

  useEffect(() => {
    const media = mediaRef.current;
    if (!media) return;

    const handleTimeUpdate = () => setCurrentTime(media.currentTime);
    const handleDurationChange = () => setDuration(media.duration);
    const handleEnded = () => playNext();

    media.addEventListener('timeupdate', handleTimeUpdate);
    media.addEventListener('durationchange', handleDurationChange);
    media.addEventListener('ended', handleEnded);

    return () => {
      media.removeEventListener('timeupdate', handleTimeUpdate);
      media.removeEventListener('durationchange', handleDurationChange);
      media.removeEventListener('ended', handleEnded);
    };
  }, [currentIndex, isAudio]);

  const handleFileUpload = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    const newMediaItems = files.map(file => ({
      id: Math.random().toString(36).substr(2, 9),
      name: file.name,
      type: file.type.startsWith('video') ? 'video' : 'audio',
      url: URL.createObjectURL(file),
      file
    }));
    setPlaylist([...playlist, ...newMediaItems]);
  };

  const exportPlaylist = async () => {
    if (playlist.length === 0) return;
    
    setIsExporting(true);
    try {
      const zip = new JSZip();
      
      // Create playlist metadata
      const metadata = {
        name: 'My Playlist',
        created: new Date().toISOString(),
        items: playlist.map(item => ({
          id: item.id,
          name: item.name,
          type: item.type
        }))
      };
      
      zip.file('playlist.json', JSON.stringify(metadata, null, 2));
      
      // Add media files
      for (const item of playlist) {
        if (item.file) {
          zip.file(`media/${item.name}`, item.file);
        }
      }
      
      // Generate and download the zip
      const content = await zip.generateAsync({ type: 'blob' });
      const url = URL.createObjectURL(content);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'playlist.zip';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (error) {
      console.error('Error exporting playlist:', error);
    } finally {
      setIsExporting(false);
    }
  };

  const togglePlay = () => {
    if (mediaRef.current) {
      if (isPlaying) {
        mediaRef.current.pause();
      } else {
        mediaRef.current.play();
      }
      setIsPlaying(!isPlaying);
    }
  };

  const toggleMute = () => {
    if (mediaRef.current) {
      mediaRef.current.muted = !isMuted;
      setIsMuted(!isMuted);
    }
  };

  const handleVolumeChange = (value: number) => {
    if (mediaRef.current) {
      mediaRef.current.volume = value;
      setVolume(value);
      setIsMuted(value === 0);
    }
  };

  const handleSeek = (value: number) => {
    if (mediaRef.current) {
      mediaRef.current.currentTime = value;
      setCurrentTime(value);
    }
  };

  const playNext = () => {
    if (currentIndex < playlist.length - 1) {
      setCurrentIndex(currentIndex + 1);
      setIsPlaying(true);
    }
  };

  const playPrevious = () => {
    if (currentIndex > 0) {
      setCurrentIndex(currentIndex - 1);
      setIsPlaying(true);
    }
  };

  return (
    <div className="max-w-4xl mx-auto p-6 bg-white rounded-xl shadow-2xl">
      <div className="mb-8">
        <div className="flex justify-between items-center mb-4">
          <h1 className="text-3xl font-bold text-gray-800">Media Player</h1>
        </div>
        
        {/* Media Player */}
        <div className="relative aspect-video bg-black rounded-lg overflow-hidden mb-4">
          {playlist.length > 0 ? (
            isAudio ? (
              <>
                <audio
                  ref={audioRef}
                  src={currentMedia?.url}
                  className="hidden"
                  autoPlay={isPlaying}
                  onPlay={() => setIsPlaying(true)}
                  onPause={() => setIsPlaying(false)}
                />
                <div className="w-full h-full flex items-center justify-center">
                  <AudioVisualizer audioElement={audioRef.current} />
                </div>
              </>
            ) : (
              <video
                ref={videoRef}
                src={currentMedia?.url}
                className="w-full h-full"
                autoPlay={isPlaying}
                onPlay={() => setIsPlaying(true)}
                onPause={() => setIsPlaying(false)}
              />
            )
          ) : (
            <div className="absolute inset-0 flex items-center justify-center text-gray-500">
              No media loaded
            </div>
          )}
        </div>

        {/* Controls */}
        <MediaControls
          isPlaying={isPlaying}
          isMuted={isMuted}
          volume={volume}
          duration={duration}
          currentTime={currentTime}
          onPlayPause={togglePlay}
          onNext={playNext}
          onPrevious={playPrevious}
          onMute={toggleMute}
          onVolumeChange={handleVolumeChange}
          onSeek={handleSeek}
          hasNext={currentIndex < playlist.length - 1}
          hasPrevious={currentIndex > 0}
        />
      </div>

      {/* Upload Section */}
      <div className="mb-8">
        <input
          type="file"
          ref={fileInputRef}
          onChange={handleFileUpload}
          className="hidden"
          multiple
          accept="audio/*,video/*"
        />
        <button
          onClick={() => fileInputRef.current?.click()}
          className="w-full py-4 border-2 border-dashed border-gray-300 rounded-lg hover:border-blue-500 transition-colors flex items-center justify-center gap-2"
        >
          <Upload className="w-5 h-5" />
          <span>Upload Media Files</span>
        </button>
      </div>

      {/* Playlist */}
      <div className="bg-gray-50 rounded-lg p-4">
        <div className="flex justify-between items-center mb-4">
          <h2 className="text-xl font-semibold">Playlist</h2>
          {playlist.length > 0 && (
            <button
              onClick={exportPlaylist}
              disabled={isExporting}
              className="flex items-center gap-2 px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg transition-colors disabled:opacity-50"
            >
              <Download className="w-5 h-5" />
              {isExporting ? 'Exporting...' : 'Export Playlist'}
            </button>
          )}
        </div>
        {playlist.length === 0 ? (
          <p className="text-gray-500 text-center py-4">No media files added yet</p>
        ) : (
          <div className="space-y-2">
            {playlist.map((item, index) => (
              <PlaylistItem
                key={item.id}
                name={item.name}
                type={item.type}
                isActive={currentIndex === index}
                onClick={() => {
                  setCurrentIndex(index);
                  setIsPlaying(true);
                }}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}