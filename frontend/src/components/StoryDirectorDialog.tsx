import { useState, useEffect, useCallback } from "react";
import { api } from "../api/client";
import { useBoardStore } from "../store/board";
import { useGenerationStore } from "../store/generation";

interface Shot {
  id: string;
  index: number;
  title: string;
  narration: string;
  image_prompt: string;
  video_prompt: string;
  camera: string;
  duration: number;
  sampleFrameRef?: { media_id: string; aspect_ratio: string };
}

export interface StoryDirectorDialogProps {
  rfId: string;
  initialShots?: Shot[];
  onClose: () => void;
}

export const STYLE_OPTIONS = [
  { value: "review", label: "Review (Quảng cáo)" },
  { value: "entertainment", label: "Giải trí / Vlog" },
  { value: "cinematic", label: "Cinematic (Phim ngắn)" },
  { value: "dance", label: "Dance / Trend" },
  { value: "education", label: "Giáo dục / Hướng dẫn" },
];

export const DURATION_OPTIONS = [
  { value: 4, label: "4 giây" },
  { value: 6, label: "6 giây" },
  { value: 8, label: "8 giây" },
  { value: 10, label: "10 giây" },
];

export const ASPECT_OPTIONS = [
  { value: "portrait", label: "Dọc (9:16)" },
  { value: "landscape", label: "Ngang (16:9)" },
  { value: "square", label: "Vuông (1:1)" },
];

export const SCENE_COUNT_OPTIONS = [
  { value: 1, label: "1 cảnh" },
  { value: 2, label: "2 cảnh" },
  { value: 3, label: "3 cảnh" },
  { value: 4, label: "4 cảnh" },
  { value: 5, label: "5 cảnh" },
  { value: 6, label: "6 cảnh" },
  { value: 7, label: "7 cảnh" },
  { value: 8, label: "8 cảnh" },
  { value: 9, label: "9 cảnh" },
  { value: 10, label: "10 cảnh" },
];

export function StoryDirectorDialog({
  rfId,
  initialShots,
  onClose,
}: StoryDirectorDialogProps) {
  const [shots, setShots] = useState<any[]>(initialShots || []);
  const [error, setError] = useState<string | null>(null);
  const [creatingVideo, setCreatingVideo] = useState(false);

  const dbId = parseInt(rfId, 10);

  // Close on Escape
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onClose]);

  const handleShotChange = useCallback(
    (index: number, field: keyof Shot, value: string | number) => {
      setShots((prev) =>
        prev.map((shot, i) =>
          i === index ? { ...shot, [field]: value } : shot
        )
      );
    },
    []
  );

  const handleSaveShots = useCallback(async () => {
    try {
      await api(`/api/nodes/story-script/${dbId}/update-shots`, {
        method: "POST",
        body: JSON.stringify({ shots }),
      });
    } catch (err: any) {
      setError(err.message || "Lỗi lưu shots");
    }
  }, [dbId, shots]);

  const handleCreateVideo = useCallback(async () => {
    setCreatingVideo(true);
    setError(null);

    try {
      // Save shots first
      await api(`/api/nodes/story-script/${dbId}/update-shots`, {
        method: "POST",
        body: JSON.stringify({ shots }),
      });

      // Create video
      let projectId: string | null = null;
      try {
        projectId = await useGenerationStore.getState().ensureProjectId();
      } catch { /* optional */ }

      await api(`/api/nodes/story-script/${dbId}/create-video`, {
        method: "POST",
        body: JSON.stringify({ projectId }),
      });

      // Refresh board to show spawned nodes
      await useBoardStore.getState().refreshBoardState();
      onClose();
    } catch (err: any) {
      setError(err.message || "Lỗi tạo video");
    } finally {
      setCreatingVideo(false);
    }
  }, [dbId, shots, onClose]);

  return (
    <div className="story-director-overlay" onClick={onClose}>
      <div
        className="story-director-dialog"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="story-director-header">
          <div className="story-director-header__left">
            <span className="story-director-header__icon">🎬</span>
            <span className="story-director-header__title">
              AI DIRECTOR PANEL (SHORT FILM)
            </span>
          </div>
          <button
            className="story-director-header__close"
            onClick={onClose}
            title="Đóng"
          >
            ✕
          </button>
        </div>

        <div className="story-director-content">
          {/* Shots list only */}
          {shots.length === 0 ? (
            <div style={{ color: "var(--muted)", textAlign: "center", padding: 20 }}>
              Chưa có phân cảnh nào. Hãy tạo kịch bản từ Node trước.
            </div>
          ) : (
            <div className="story-director-shots" style={{ borderTop: "none", paddingTop: 0, marginTop: 0 }}>
              {shots.map((shot, i) => (
                <div key={shot.id} className="shot-card">
                  <div className="shot-card__header">
                    <span className="shot-card__title">
                      PHÂN CẢNH {i + 1}
                    </span>
                    <span className="shot-card__duration">
                      {shot.duration}s
                    </span>
                  </div>

                  {/* Narration (Vietnamese, red/orange) */}
                  <textarea
                    className="shot-card__narration"
                    value={shot.narration}
                    onChange={(e) =>
                      handleShotChange(i, "narration", e.target.value)
                    }
                    onBlur={handleSaveShots}
                    placeholder="Lời thoại / voiceover (tiếng Việt)..."
                    rows={2}
                    disabled={creatingVideo}
                  />

                  {/* Image prompt (English, white) */}
                  <textarea
                    className="shot-card__image-prompt"
                    value={shot.image_prompt}
                    onChange={(e) =>
                      handleShotChange(i, "image_prompt", e.target.value)
                    }
                    onBlur={handleSaveShots}
                    placeholder="Image prompt (English)..."
                    rows={2}
                    disabled={creatingVideo}
                  />

                  {/* Camera (English, small gray) */}
                  <textarea
                    className="shot-card__camera"
                    value={shot.camera}
                    onChange={(e) =>
                      handleShotChange(i, "camera", e.target.value)
                    }
                    onBlur={handleSaveShots}
                    placeholder="Camera direction (English)..."
                    rows={1}
                    disabled={creatingVideo}
                  />
                </div>
              ))}

              {/* Error */}
              {error && (
                <div className="story-director-error">{error}</div>
              )}

              {/* Create video button */}
              <button
                className="story-director-create-btn"
                onClick={handleCreateVideo}
                disabled={creatingVideo}
              >
                {creatingVideo ? (
                  <>
                    <span className="story-director-spinner" />
                    Đang tạo video...
                  </>
                ) : (
                  <>
                    <span>✦</span> TẠO VIDEO TỪ KỊCH BẢN
                  </>
                )}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
