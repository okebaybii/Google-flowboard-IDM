import React, { useEffect, useRef, useState } from "react";
import { useBoardStore, type FlowboardNodeData, type FlowNode } from "../store/board";
import { useSettingsStore } from "../store/settings";
import { mediaUrl, patchNode } from "../api/client";

interface VideoAssemblyDialogProps {
  rfId: string;
  data: FlowboardNodeData;
  onClose: () => void;
}

export function VideoAssemblyDialog({ rfId, data, onClose }: VideoAssemblyDialogProps) {
  const nodes = useBoardStore((s) => s.nodes);
  const edges = useBoardStore((s) => s.edges);
  const imageModel = useSettingsStore((s) => s.imageModel);
  const videoQuality = useSettingsStore((s) => s.videoQuality);
  const videoModel = useSettingsStore((s) => s.videoModel);
  const omniFlashDuration = useSettingsStore((s) => s.omniFlashDuration);

  const [orderedVideos, setOrderedVideos] = useState<FlowNode[]>([]);
  const [audioMediaId, setAudioMediaId] = useState<string | null>(
    (data.audioMediaId as string) || null
  );
  const [audioFilename, setAudioFilename] = useState<string | null>(
    (data.audioFilename as string) || null
  );
  const [uploadingAudio, setUploadingAudio] = useState(false);
  const [audioPlaying, setAudioPlaying] = useState(false);
  const isRunning = data.status === "running";
  const [assembling, setAssembling] = useState(isRunning);

  useEffect(() => {
    setAssembling(data.status === "running");
  }, [data.status]);

  const [error, setError] = useState<string | null>(null);

  // Trạng thái kéo thả
  const [draggedIndex, setDraggedIndex] = useState<number | null>(null);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  const [batchGenerating, setBatchGenerating] = useState(
    Boolean(data.batchGenerating) || false
  );
  const [autoAssembleAfterBatch, setAutoAssembleAfterBatch] = useState(
    Boolean(data.autoAssembleAfterBatch) || false
  );
  const [batchVideoAspectRatio, setBatchVideoAspectRatio] = useState(
    data.batchVideoAspectRatio || "VIDEO_ASPECT_RATIO_PORTRAIT"
  );

  const [retryFailedClips, setRetryFailedClips] = useState(
    Boolean(data.retryFailedClips) || false
  );
  const autoAssembleTriggeredRef = useRef(false);
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Lọc và quét các video node kết nối để phục vụ dependency key tối ưu
  const upstreamEdges = edges.filter((e) => e.target === rfId);
  const upstreamNodeIds = upstreamEdges.map((e) => e.source);
  const connectedVideos = nodes.filter(
    (n) => upstreamNodeIds.includes(n.id) && n.data.type === "video"
  );
  
  const totalConnected = connectedVideos.length;
  const unrenderedCount = connectedVideos.filter((n) => !n.data.mediaId).length;
  const renderedCount = connectedVideos.filter((n) => !!n.data.mediaId).length;
  const runningCount = connectedVideos.filter(
    (n) => n.data.status === "queued" || n.data.status === "running"
  ).length;
  const erroredVideos = connectedVideos.filter((n) => n.data.status === "error" && !n.data.mediaId);
  const errorCount = erroredVideos.length;
  const hasActiveClipRequests = runningCount > 0;
  const shouldShowBatchProgress = batchGenerating || hasActiveClipRequests;
  
  const connectedMediaIdsKey = connectedVideos
    .map((n) => `${n.id}:${n.data.mediaId || ""}:${n.data.status || ""}:${n.data.error || ""}`)
    .join(",");

  // Tự động theo dõi batch: chỉ hoàn tất khi mọi video đã có mediaId,
  // hoặc dừng với lỗi rõ ràng khi không còn request chạy nhưng vẫn thiếu clip.
  useEffect(() => {
    const autoAssemblePending = autoAssembleAfterBatch || Boolean(data.autoAssembleAfterBatch);
    if (!batchGenerating && !autoAssemblePending) return;

    if (unrenderedCount === 0 && totalConnected > 0) {
      setBatchGenerating(false);
      setAutoAssembleAfterBatch(false);
      
      const currentBatchGen = Boolean(data.batchGenerating);
      const currentAutoAssemble = Boolean(data.autoAssembleAfterBatch);
      if (currentBatchGen || currentAutoAssemble) {
        useBoardStore.getState().updateNodeData(rfId, {
          batchGenerating: false,
          autoAssembleAfterBatch: false,
        });
        const dbId = parseInt(rfId, 10);
        if (!isNaN(dbId)) {
          void patchNode(dbId, {
            data: { batchGenerating: false, autoAssembleAfterBatch: false },
          });
        }
      }

      if (!autoAssemblePending) {
        if (pollIntervalRef.current) {
          clearInterval(pollIntervalRef.current);
          pollIntervalRef.current = null;
        }
      }
      return;
    }

    if (runningCount === 0 && unrenderedCount > 0 && errorCount > 0) {
      setBatchGenerating(false);
      setAutoAssembleAfterBatch(false);

      const currentBatchGen = Boolean(data.batchGenerating);
      const currentAutoAssemble = Boolean(data.autoAssembleAfterBatch);
      if (currentBatchGen || currentAutoAssemble) {
        useBoardStore.getState().updateNodeData(rfId, {
          batchGenerating: false,
          autoAssembleAfterBatch: false,
        });
        const dbId = parseInt(rfId, 10);
        if (!isNaN(dbId)) {
          void patchNode(dbId, {
            data: { batchGenerating: false, autoAssembleAfterBatch: false },
          });
        }
      }

      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
        pollIntervalRef.current = null;
      }
      const firstErrored = connectedVideos.find((n) => n.data.status === "error");
      const firstError = firstErrored?.data.error || "Một số clip tạo thất bại.";
      setError(`Batch dừng: ${firstError}`);
    }
  }, [batchGenerating, unrenderedCount, totalConnected, autoAssembleAfterBatch, assembling, runningCount, errorCount, connectedMediaIdsKey]);

  // Dọn dẹp poller khi unmount
  useEffect(() => {
    return () => {
      if (pollIntervalRef.current) {
        clearInterval(pollIntervalRef.current);
      }
    };
  }, []);

  const startBoardPolling = () => {
    if (pollIntervalRef.current) return;
    pollIntervalRef.current = setInterval(async () => {
      try {
        await useBoardStore.getState().refreshBoardState();
      } catch (err) {
        console.warn("Lỗi poll trạng thái bảng:", err);
      }
    }, 1500);
  };

  useEffect(() => {
    if (hasActiveClipRequests || Boolean(data.batchGenerating) || data.status === "running") {
      if (Boolean(data.batchGenerating)) {
        setBatchGenerating(true);
      }
      startBoardPolling();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasActiveClipRequests, data.batchGenerating, data.status]);

  const startBatchGenerate = async (shouldAutoAssemble = false) => {
    setAutoAssembleAfterBatch(shouldAutoAssemble);
    const dbId = parseInt(rfId, 10);
    autoAssembleTriggeredRef.current = false;
    setError(null);
    try {
      const response = await fetch(`/api/video-assembly/node/${dbId}/generate-all`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          image_model: imageModel,
          video_quality: videoQuality,
          video_model: videoModel,
          omni_flash_duration: omniFlashDuration,
          auto_assemble: shouldAutoAssemble,
          batch_video_aspect_ratio: batchVideoAspectRatio,
          retry_failed: retryFailedClips,
        }),
      });
      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${response.status}`);
      }
      setBatchGenerating(true);
      useBoardStore.getState().updateNodeData(rfId, {
        batchGenerating: true,
        autoAssembleAfterBatch: shouldAutoAssemble,
        batchVideoAspectRatio,
        retryFailedClips,
      });
      if (!isNaN(dbId)) {
        void patchNode(dbId, {
          data: {
            batchGenerating: true,
            autoAssembleAfterBatch: shouldAutoAssemble,
            batchVideoAspectRatio,
            retryFailedClips,
          },
        });
      }
      startBoardPolling();
    } catch (err: any) {
      alert(`Lỗi khởi tạo hàng loạt: ${err.message || err}`);
      setBatchGenerating(false);
      setAutoAssembleAfterBatch(false);
      useBoardStore.getState().updateNodeData(rfId, {
        batchGenerating: false,
        autoAssembleAfterBatch: false,
      });
      if (!isNaN(dbId)) {
        void patchNode(dbId, {
          data: { batchGenerating: false, autoAssembleAfterBatch: false },
        });
      }
    }
  };

  // 1. Quét và sắp xếp các video node đầu vào (Tránh kích hoạt thừa)
  useEffect(() => {
    // Không sắp xếp lại khi người dùng đang thực hiện kéo thả dở dang
    if (draggedIndex !== null) return;

    const savedOrder = (data.videoOrder as string[]) || [];

    const sorted = [...connectedVideos].sort((a, b) => {
      const idxA = savedOrder.indexOf(a.id);
      const idxB = savedOrder.indexOf(b.id);
      if (idxA !== -1 && idxB !== -1) return idxA - idxB;
      if (idxA !== -1) return -1;
      if (idxB !== -1) return 1;
      return a.position.x - b.position.x;
    });

    setOrderedVideos(sorted);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectedMediaIdsKey, rfId, JSON.stringify(data.videoOrder), draggedIndex]);

  // Keyboard shortcut: Esc to close
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !assembling) {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose, assembling]);

  // Nghe thử âm thanh tự động reset khi hết tệp
  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    const onEnded = () => setAudioPlaying(false);
    el.addEventListener("ended", onEnded);
    return () => {
      if (el) el.removeEventListener("ended", onEnded);
    };
  }, [audioMediaId]);

  // Lưu thứ tự clip vào cả Zustand Store lẫn Database Backend
  const saveVideoOrder = async (newOrder: FlowNode[]) => {
    const ids = newOrder.map((n) => n.id);
    
    // Cập nhật Zustand frontend
    useBoardStore.getState().updateNodeData(rfId, { videoOrder: ids });

    // Đồng bộ vào cơ sở dữ liệu database qua patchNode
    const dbId = parseInt(rfId, 10);
    if (!isNaN(dbId)) {
      try {
        await patchNode(dbId, { data: { videoOrder: ids } });
      } catch (err) {
        console.error("Lỗi đồng bộ thứ tự video lên server:", err);
      }
    }
  };

  // --- HTML5 Drag and Drop Handlers ---
  const handleDragStart = (e: React.DragEvent, index: number) => {
    setDraggedIndex(index);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(index));
  };

  const handleDragOver = (e: React.DragEvent, index: number) => {
    e.preventDefault(); // Bắt buộc để cho phép thả (Drop)
    if (draggedIndex === null || draggedIndex === index) return;

    // Hoán đổi vị trí của các phần tử trong danh sách ngay lập tức khi đang di chuột
    const newOrder = [...orderedVideos];
    const temp = newOrder[draggedIndex];
    newOrder.splice(draggedIndex, 1);
    newOrder.splice(index, 0, temp);

    setDraggedIndex(index);
    setOrderedVideos(newOrder);
  };

  const handleDragEnd = () => {
    if (draggedIndex !== null) {
      setDraggedIndex(null);
      // Persist thứ tự cuối cùng sau khi thả chuột
      void saveVideoOrder(orderedVideos);
    }
  };

  // Phát / Tạm dừng tệp nghe thử âm thanh
  const togglePlayAudio = () => {
    if (!audioRef.current) return;
    if (audioPlaying) {
      audioRef.current.pause();
      setAudioPlaying(false);
    } else {
      audioRef.current.play().catch((err) => console.error("Play failed", err));
      setAudioPlaying(true);
    }
  };

  // Tải lên tệp âm thanh nền
  const handleAudioUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    // Validate định dạng âm thanh
    const mime = file.type.toLowerCase();
    const allowedExtensions = [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma", ".opus"];
    const fileExt = file.name.substring(file.name.lastIndexOf(".")).toLowerCase();
    const isAudio = mime.startsWith("audio/") || allowedExtensions.includes(fileExt);
    if (!isAudio) {
      alert("Định dạng âm thanh không hỗ trợ. Vui lòng tải lên tệp âm thanh hợp lệ (như .mp3, .wav, .m4a, .flac, .aac, .ogg)");
      return;
    }

    setUploadingAudio(true);
    const formData = new FormData();
    formData.append("file", file);

    const dbId = parseInt(rfId, 10);
    if (!isNaN(dbId)) {
      formData.append("node_id", String(dbId));
    }

    try {
      const response = await fetch("/api/video-assembly/upload-audio", {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || `HTTP ${response.status}`);
      }

      const res = await response.json();
      setAudioMediaId(res.media_id);
      setAudioFilename(file.name);

      // Cập nhật dữ liệu node trong Store
      useBoardStore.getState().updateNodeData(rfId, {
        audioMediaId: res.media_id,
        audioFilename: file.name,
      });

      // Đồng bộ DB
      if (!isNaN(dbId)) {
        await patchNode(dbId, {
          data: {
            audioMediaId: res.media_id,
            audioFilename: file.name,
          },
        });
      }
    } catch (err: any) {
      console.error("Audio upload error:", err);
      alert(`Lỗi tải nhạc nền lên backend: ${err.message || err}`);
    } finally {
      setUploadingAudio(false);
    }
  };

  // Gỡ bỏ tệp nhạc nền
  const handleRemoveAudio = () => {
    setAudioMediaId(null);
    setAudioFilename(null);
    setAudioPlaying(false);
    
    useBoardStore.getState().updateNodeData(rfId, {
      audioMediaId: null,
      audioFilename: null,
    });

    const dbId = parseInt(rfId, 10);
    if (!isNaN(dbId)) {
      void patchNode(dbId, {
        data: {
          audioMediaId: null,
          audioFilename: null,
        },
      });
    }

    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  // Kích hoạt tiến trình ghép nối video ở backend
  async function handleAssemble() {
    if (orderedVideos.length === 0) {
      alert("Vui lòng kết nối ít nhất 1 clip video đầu vào từ canvas.");
      return;
    }

    const hasUnrendered = orderedVideos.some((n) => !n.data.mediaId);
    if (hasUnrendered) {
      alert("Một số clip video chưa được tạo (generate). Vui lòng tạo tất cả clip trước khi ghép nối.");
      return;
    }

    setAssembling(true);
    setError(null);

    // Cập nhật trạng thái node sang running để hiển thị loading trên Canvas
    useBoardStore.getState().updateNodeData(rfId, { status: "running" });

    try {
      const dbId = parseInt(rfId, 10);
      const videoOrderIds = orderedVideos.map((n) => n.id);

      const response = await fetch(`/api/video-assembly/node/${dbId}/assemble`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          video_order: videoOrderIds,
          audio_media_id: audioMediaId,
        }),
      });

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || `HTTP ${response.status}`);
      }

      const res = await response.json();

      // Cập nhật node dữ liệu thành công
      useBoardStore.getState().updateNodeData(rfId, {
        status: "done",
        mediaId: res.mediaId,
        mediaIds: [res.mediaId],
        variantCount: 1,
        aspectRatio: "16:9",
        audioMediaId: audioMediaId,
        videoOrder: videoOrderIds,
        renderedAt: new Date().toISOString(),
      });

      // Refresh board state
      await useBoardStore.getState().refreshBoardState();

      // Đóng modal
      onClose();
    } catch (err: any) {
      console.error("Assembly compilation error:", err);
      setError(err.message || String(err));
      useBoardStore.getState().updateNodeData(rfId, { status: "error" });
    } finally {
      setAssembling(false);
    }
  }


  return (
    <div
      className="gen-dialog-backdrop"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget && !assembling) onClose();
      }}
    >
      <div
        className="gen-dialog video-assembly-dialog"
        role="dialog"
        aria-labelledby="video-assembly-title"
        aria-modal="true"
        ref={dialogRef}
        style={{ maxWidth: 600, width: "100%" }}
      >
        {/* Header */}
        <div className="gen-dialog__header">
          <div>
            <h2 id="video-assembly-title" className="gen-dialog__title">
              🎬 Cấu hình Ghép nối Video
            </h2>
            <span className="gen-dialog__subtitle">
              Node #{data.shortId} · {totalConnected} clip được kết nối
            </span>
          </div>
          <button
            className="gen-dialog__close"
            onClick={onClose}
            disabled={assembling}
            aria-label="Đóng (Escape)"
          >
            esc
          </button>
        </div>

        {/* Output Preview (if already compiled) */}
        {data.status === "done" && data.mediaId && (
          <div className="gen-dialog__field" style={{
            background: "rgba(16, 185, 129, 0.08)",
            border: "1px solid rgba(16, 185, 129, 0.2)",
            borderRadius: 10,
            padding: 14,
            marginBottom: 16,
            display: "flex",
            flexDirection: "column",
            gap: 10
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: "#10b981", display: "flex", alignItems: "center", gap: 6 }}>
                <span>🎉</span> Phim đã ghép nối thành công!
              </div>
              <button
                type="button"
                onClick={() => {
                  const safeTitle = (data.title || "video_assembly").replace(/[^A-Za-z0-9_-]+/g, "_");
                  const a = document.createElement("a");
                  a.href = mediaUrl(data.mediaId!);
                  a.download = `${safeTitle}-${data.shortId}.mp4`;
                  document.body.appendChild(a);
                  a.click();
                  a.remove();
                }}
                style={{
                  background: "linear-gradient(135deg, #10b981 0%, #059669 100%)",
                  color: "#fff",
                  border: "none",
                  borderRadius: 6,
                  padding: "4px 10px",
                  fontSize: 11,
                  fontWeight: 600,
                  cursor: "pointer",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 4
                }}
              >
                Tải xuống 📥
              </button>
            </div>
            <div style={{ display: "flex", justifyContent: "center", width: "100%" }}>
              <video
                src={mediaUrl(data.mediaId)}
                controls
                style={{
                  width: data.aspectRatio === "9:16" ? "auto" : "100%",
                  height: data.aspectRatio === "9:16" ? "240px" : "auto",
                  maxHeight: 240,
                  borderRadius: 8,
                  background: "#000",
                  boxShadow: "0 4px 12px rgba(0,0,0,0.3)"
                }}
              />
            </div>
          </div>
        )}

        {/* Section 1: Clip Sequencer */}
        <div className="gen-dialog__field">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 2 }}>
            <label className="gen-dialog__label" style={{ marginBottom: 0 }}>Thứ tự ghép clip video</label>
            {totalConnected > 1 && (
              <span style={{ fontSize: 10, color: "var(--muted)" }}>💡 Kéo thả trực tiếp các dòng để sắp xếp thứ tự</span>
            )}
          </div>

          {/* Batch Generation Control and Progress Bar */}
          {totalConnected > 0 && (
            <div style={{ marginBottom: 12, display: "flex", flexDirection: "column", gap: 8 }}>
              {unrenderedCount > 0 && !batchGenerating && (
                <div style={{
                  background: "rgba(124, 92, 255, 0.08)",
                  border: "1px solid rgba(124, 92, 255, 0.18)",
                  borderRadius: 10,
                  padding: 12,
                  display: "flex",
                  flexDirection: "column",
                  gap: 10,
                }}>
                  <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text)" }}>
                    ⚙️ Cấu hình trước khi tạo video hàng loạt
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    <span style={{ fontSize: 11, color: "var(--muted)", fontWeight: 600 }}>Kích thước video</span>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      {[
                        ["VIDEO_ASPECT_RATIO_PORTRAIT", "9:16 dọc TikTok/Reels"],
                        ["VIDEO_ASPECT_RATIO_LANDSCAPE", "16:9 ngang YouTube"],
                      ].map(([value, label]) => (
                        <button
                          key={value}
                          type="button"
                          onClick={() => setBatchVideoAspectRatio(value)}
                          className={`aspect-chip${batchVideoAspectRatio === value ? " aspect-chip--active" : ""}`}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  </div>

                  {errorCount > 0 && (
                    <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--muted)" }}>
                      <input
                        type="checkbox"
                        checked={retryFailedClips}
                        onChange={(e) => setRetryFailedClips(e.target.checked)}
                      />
                      Tạo lại cả các clip đang báo lỗi
                    </label>
                  )}
                </div>
              )}

              {unrenderedCount > 0 && !batchGenerating && (
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <button
                    type="button"
                    onClick={() => startBatchGenerate(false)}
                    disabled={assembling}
                    style={{
                      background: "linear-gradient(135deg, #7c5cff 0%, #a05cff 100%)",
                      color: "#fff",
                      border: "none",
                      borderRadius: 6,
                      padding: "8px 14px",
                      fontSize: 12,
                      fontWeight: 600,
                      cursor: "pointer",
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 6,
                      transition: "all 0.15s ease",
                      boxShadow: "0 2px 8px rgba(124, 92, 255, 0.25)"
                    }}
                  >
                    ⚡ Tạo tất cả {unrenderedCount} clip còn thiếu
                  </button>
                  <button
                    type="button"
                    onClick={() => startBatchGenerate(true)}
                    disabled={assembling}
                    style={{
                      background: "linear-gradient(135deg, #10b981 0%, #06b6d4 100%)",
                      color: "#fff",
                      border: "none",
                      borderRadius: 6,
                      padding: "8px 14px",
                      fontSize: 12,
                      fontWeight: 700,
                      cursor: "pointer",
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 6,
                      transition: "all 0.15s ease",
                      boxShadow: "0 2px 8px rgba(6, 182, 212, 0.25)"
                    }}
                  >
                    🚀 Tạo xong tự ghép
                  </button>
                </div>
              )}

              {shouldShowBatchProgress && (
                <div style={{
                  background: "var(--panel-high)",
                  border: "1px solid rgba(124, 92, 255, 0.3)",
                  borderRadius: 8,
                  padding: 12,
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                  boxShadow: "0 0 15px rgba(124, 92, 255, 0.15)"
                }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 12 }}>
                    <span style={{ fontWeight: 600, color: "var(--accent)", display: "flex", alignItems: "center", gap: 6 }}>
                      <span className="video-assembly__spinner" style={{ width: 12, height: 12, marginRight: 6 }} />
                      {autoAssembleAfterBatch ? "Đang tạo clip, xong sẽ tự ghép..." : "Đang tạo video hàng loạt..."}
                    </span>
                    <span style={{ color: "var(--muted)" }}>
                      {renderedCount}/{totalConnected} clip ({Math.round((renderedCount / totalConnected) * 100)}%)
                    </span>
                  </div>
                  <div style={{ fontSize: 11, color: "var(--muted)", display: "flex", gap: 10, flexWrap: "wrap" }}>
                    <span>✅ Xong: {renderedCount}</span>
                    <span>⏳ Đang chạy: {runningCount}</span>
                    <span>🧩 Còn thiếu: {unrenderedCount}</span>
                    {errorCount > 0 && <span style={{ color: "#fca5a5" }}>⚠️ Lỗi: {errorCount}</span>}
                  </div>
                  <div style={{ background: "rgba(255,255,255,0.06)", height: 6, borderRadius: 3, overflow: "hidden" }}>
                    <div style={{
                      background: "linear-gradient(90deg, #7c5cff 0%, #a05cff 100%)",
                      height: "100%",
                      width: `${Math.round((renderedCount / totalConnected) * 100)}%`,
                      transition: "width 0.3s ease"
                    }} />
                  </div>
                </div>
              )}
            </div>
          )}

          {totalConnected === 0 ? (
            <div className="video-assembly__empty-list">
              <span>⚠️ Chưa có clip video đầu vào được kết nối.</span>
              <p>Hãy liên kết đầu ra của các Node Video tới Node Video Assembly này trên Canvas.</p>
            </div>
          ) : (
            <div className="video-assembly__clip-list">
              {orderedVideos.map((videoNode, idx) => {
                const mid = videoNode.data.mediaId;
                const isReady = !!mid;
                const promptText = videoNode.data.prompt || videoNode.data.title || "Clip không có tiêu đề";
                const isDraggingThis = draggedIndex === idx;

                return (
                  <div
                    key={videoNode.id}
                    className={`video-assembly__clip-item ${isDraggingThis ? "video-assembly__clip-item--dragging" : ""}`}
                    draggable={!assembling}
                    onDragStart={(e) => handleDragStart(e, idx)}
                    onDragOver={(e) => handleDragOver(e, idx)}
                    onDragEnd={handleDragEnd}
                  >
                    {/* Drag Handle */}
                    <div className="video-assembly__clip-drag-handle">⋮⋮</div>
                    
                    {/* Index & Thumb */}
                    <div className="video-assembly__clip-index">{idx + 1}</div>
                    <div className="video-assembly__clip-thumb-container">
                      {isReady ? (
                        <video
                          src={mediaUrl(mid!)}
                          className="video-assembly__clip-thumb-video"
                          muted
                          playsInline
                          loop
                          onMouseOver={(e) => (e.target as HTMLVideoElement).play()}
                          onMouseOut={(e) => {
                            const v = e.target as HTMLVideoElement;
                            v.pause();
                            v.currentTime = 0;
                          }}
                        />
                      ) : (
                        <div className="video-assembly__clip-thumb-placeholder">⏳ Trống</div>
                      )}
                    </div>

                    {/* Meta info */}
                    <div className="video-assembly__clip-meta">
                      <div className="video-assembly__clip-title">
                        {videoNode.data.title || `Video Node #${videoNode.data.shortId}`}
                      </div>
                      <div className="video-assembly__clip-prompt" title={promptText}>
                        {promptText.length > 55 ? promptText.substring(0, 55) + "…" : promptText}
                      </div>
                      {!isReady && videoNode.data.status && (
                        <div style={{ marginTop: 4, fontSize: 10, color: videoNode.data.status === "error" ? "#fca5a5" : "var(--muted)" }}>
                          {videoNode.data.status === "error"
                            ? `⚠️ ${videoNode.data.errorHint || videoNode.data.error || "Tạo clip thất bại"}`
                            : videoNode.data.status === "queued" || videoNode.data.status === "running"
                              ? `⏳ ${videoNode.data.status === "queued" ? "Đang chờ" : "Đang tạo"}`
                              : "🧩 Chưa có media"}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Section 2: Background Audio (Sleek Compact Version) */}
        <div className="gen-dialog__field">
          <div
            className="video-assembly__audio-row"
            style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 4 }}
          >
            <label
              className="gen-dialog__label"
              style={{ marginBottom: 0, textTransform: "uppercase", fontSize: 10 }}
            >
              Nhạc nền:
            </label>
            <input
              type="file"
              ref={fileInputRef}
              onChange={handleAudioUpload}
              accept="audio/*, .mp3, .wav, .m4a, .aac, .flac, .ogg, .wma, .opus"
              style={{ display: "none" }}
              disabled={assembling || uploadingAudio}
            />

            {audioMediaId ? (
              <div
                className="video-assembly__audio-compact"
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  background: "var(--panel-high)",
                  border: "1px solid var(--border)",
                  borderRadius: 6,
                  padding: "4px 10px",
                  gap: 8,
                }}
              >
                <span style={{ fontSize: 12 }}>🎵</span>
                <span
                  style={{
                    fontSize: 11,
                    color: "var(--text)",
                    fontWeight: 500,
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    maxWidth: 200,
                  }}
                  title={audioFilename || "Tệp nhạc nền"}
                >
                  {audioFilename || `Nhạc nền (#${audioMediaId.substring(0, 6)})`}
                </span>

                <audio src={mediaUrl(audioMediaId)} ref={audioRef} style={{ display: "none" }} />

                <button
                  type="button"
                  onClick={togglePlayAudio}
                  style={{
                    background: "none",
                    border: "none",
                    cursor: "pointer",
                    fontSize: 11,
                    color: "var(--muted)",
                    padding: 2,
                    display: "flex",
                    alignItems: "center",
                  }}
                  title={audioPlaying ? "Tạm dừng nghe thử" : "Nghe thử nhạc nền"}
                >
                  {audioPlaying ? "⏸" : "▶"}
                </button>

                <button
                  type="button"
                  onClick={handleRemoveAudio}
                  disabled={assembling}
                  style={{
                    background: "none",
                    border: "none",
                    cursor: "pointer",
                    fontSize: 10,
                    color: "var(--muted)",
                    padding: 2,
                    display: "flex",
                    alignItems: "center",
                  }}
                  title="Gỡ bỏ"
                >
                  ❌
                </button>
              </div>
            ) : (
              <button
                type="button"
                className="video-assembly__audio-import-btn"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploadingAudio || assembling}
                style={{
                  background: "rgba(124, 92, 255, 0.1)",
                  border: "1px solid rgba(124, 92, 255, 0.3)",
                  color: "var(--text)",
                  borderRadius: 6,
                  padding: "6px 12px",
                  fontSize: 12,
                  cursor: "pointer",
                  display: "inline-flex",
                  alignItems: "center",
                  fontWeight: 500,
                  transition: "all 0.15s ease",
                }}
                title="Tải lên nhạc nền (Hỗ trợ .mp3, .wav, .m4a, .flac, .aac, .ogg...)"
              >
                {uploadingAudio ? (
                  <>
                    <div
                      className="video-assembly__spinner"
                      style={{ width: 12, height: 12, marginRight: 6 }}
                    />
                    Đang tải nhạc...
                  </>
                ) : (
                  <>
                    <span style={{ marginRight: 6 }}>🎵</span> Nhập nhạc nền
                  </>
                )}
              </button>
            )}
          </div>
        </div>

        {/* Error message */}
        {error && (
          <div className="video-assembly__error-message">
            <span>❌ Lỗi ghép nối:</span>
            <p>{error}</p>
          </div>
        )}

        {/* Progress Display */}
        {assembling && (
          <div style={{
            background: "rgba(124, 92, 255, 0.08)",
            border: "1px solid rgba(124, 92, 255, 0.2)",
            borderRadius: 8,
            padding: 12,
            marginBottom: 12,
            display: "flex",
            flexDirection: "column",
            gap: 8,
            boxShadow: "0 2px 10px rgba(124, 92, 255, 0.05)"
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 12 }}>
              <span style={{ fontWeight: 600, color: "var(--accent)", display: "flex", alignItems: "center", gap: 6 }}>
                <span className="video-assembly__spinner" style={{ width: 12, height: 12, marginRight: 6 }} />
                Đang tiến hành ghép nối video...
              </span>
              <span style={{ fontWeight: 700, color: "var(--accent)" }}>
                {typeof data.assemblyProgress === "number" ? data.assemblyProgress : 0}%
              </span>
            </div>
            <div style={{ background: "rgba(255, 255, 255, 0.06)", height: 6, borderRadius: 3, overflow: "hidden" }}>
              <div style={{
                width: `${typeof data.assemblyProgress === "number" ? data.assemblyProgress : 0}%`,
                height: "100%",
                background: "linear-gradient(90deg, #7c5cff 0%, #a05cff 100%)",
                borderRadius: 3,
                transition: "width 0.3s ease"
              }} />
            </div>
          </div>
        )}

        {/* Footer actions */}
        <div className="gen-dialog__footer" style={{ marginTop: 8 }}>
          <div style={{ flex: 1 }} />
          <button
            type="button"
            className="social-dialog__btn social-dialog__btn--cancel"
            onClick={onClose}
            disabled={assembling}
          >
            Hủy bỏ
          </button>
          <button
            type="button"
            className="social-dialog__btn social-dialog__btn--post"
            style={{
              background: "linear-gradient(135deg, #7c5cff 0%, #a05cff 100%)",
              color: "#fff",
              border: "none",
              fontWeight: 600,
              padding: "0 20px",
            }}
            onClick={handleAssemble}
            disabled={assembling || totalConnected === 0 || uploadingAudio}
          >
            {assembling ? (
              <>
                <div
                  className="video-assembly__spinner"
                  style={{
                    width: 14,
                    height: 14,
                    borderColor: "#fff",
                    borderBottomColor: "transparent",
                    marginRight: 8,
                  }}
                />
                Đang ghép nối ({typeof data.assemblyProgress === "number" ? data.assemblyProgress : 0}%)
              </>
            ) : (
              "Bắt đầu ghép nối 🎬"
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
