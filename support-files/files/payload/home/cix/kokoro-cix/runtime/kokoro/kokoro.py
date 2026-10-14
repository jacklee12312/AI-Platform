# ---------------------------------------------------------------------
# Copyright 2025 Cix Technology Group Co., Ltd.  All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# ---------------------------------------------------------------------
import re
import torch
import numpy as np
import time
import wave
from dataclasses import dataclass
from typing import Dict, Optional, Union
import json
import os
import sys
import onnxruntime

def MNNDataType2NumpyDataType(data_type):
    if data_type == MNN.Halide_Type_Uint8:
        return np.uint8
    elif data_type == MNN.Halide_Type_Double:
        return np.float64
    elif data_type == MNN.Halide_Type_Int:
        return np.int32
    elif data_type == MNN.Halide_Type_Int64:
        return np.int64
    else:
        return np.float32

def createTensor(tensor, input_data=None, empty=False):
    shape = tensor.getShape()
    data_type = tensor.getDataType()
    dtype = MNNDataType2NumpyDataType(data_type)
    if input_data is None:
        if empty:
            data = np.zeros(shape, dtype=dtype)
        else:
            data = np.ones(shape, dtype=dtype)
    else:
        data = input_data.astype(dtype)
    return MNN.Tensor(shape, tensor.getDataType(), data.copy(), tensor.getDimensionType())

class KModel(torch.nn.Module):
    def __init__(self, config_path, use_npu=True, device='cpu'):
        super().__init__()
        self.device = device
        self.use_npu = use_npu
        with open(config_path, 'r', encoding='utf-8') as r:
            config = json.load(r)
            bert_model_path = config['bert']
            predictor_model_path = config['predictor']
            text_encoder_model_path = config['text_encoder']
            decoder00_model_path = config['decoder00']
            decoder01_model_path = config['decoder01']
            decoder1_model_path = config['decoder1']
            self.vocab = config['vocab']
            self.context_length = config['max_position_embeddings']

        if use_npu:
            _abs_path = os.path.join(os.getcwd(), "../../../../utils")
            sys.path.append(_abs_path)
            from NOE_Engine import EngineInfer
            global EngineInfer
            self.npu_session0 = EngineInfer(bert_model_path)
            self.npu_session1 = EngineInfer(predictor_model_path)
            self.npu_session2 = EngineInfer(text_encoder_model_path)
            self.npu_session3 = EngineInfer(decoder00_model_path)
            self.npu_session4 = EngineInfer(decoder01_model_path)

            # The current delivery image does not ship an MNN Python binding.
            # Keep the five CIX-compiled subgraphs on the NPU and run only the
            # final variable-shape decoder with ONNX Runtime on the CPU.
            session_opts = onnxruntime.SessionOptions()
            session_opts.inter_op_num_threads = 4
            session_opts.intra_op_num_threads = 4
            self.ort_session5 = onnxruntime.InferenceSession(
                decoder1_model_path,
                sess_options=session_opts,
                providers=['CPUExecutionProvider'],
            )
        else:
            session_opts = onnxruntime.SessionOptions()
            session_opts.inter_op_num_threads = 4
            session_opts.intra_op_num_threads = 4
            self.ort_session0 = onnxruntime.InferenceSession(bert_model_path,
                                                            sess_options=session_opts,
                                                            providers=['CPUExecutionProvider'])
            self.ort_session1 = onnxruntime.InferenceSession(predictor_model_path, sess_options=session_opts, providers=['CPUExecutionProvider'])
            self.ort_session2 = onnxruntime.InferenceSession(text_encoder_model_path, sess_options=session_opts, providers=['CPUExecutionProvider'])
            self.ort_session3 = onnxruntime.InferenceSession(decoder00_model_path, sess_options=session_opts, providers=['CPUExecutionProvider'])
            self.ort_session4 = onnxruntime.InferenceSession(decoder01_model_path, sess_options=session_opts, providers=['CPUExecutionProvider'])
            self.ort_session5 = onnxruntime.InferenceSession(decoder1_model_path, sess_options=session_opts, providers=['CPUExecutionProvider'])

    @dataclass
    class Output:
        audio: torch.FloatTensor
        pred_dur: Optional[torch.LongTensor] = None

    def forward_with_tokens(
        self,
        tokens: torch.LongTensor,
        ref_s: torch.FloatTensor,
        speed: float = 1
    ) -> tuple[torch.FloatTensor, torch.LongTensor]:
        start_time = time.perf_counter()
        device = ref_s.device
        #tokens = torch.LongTensor([[0, *tokens, 0]]).to(device)
        input_lengths = torch.LongTensor([tokens.shape[-1]]).to(device)
        max_seq = 200
        #==============================pad token=================================
        pad_tokens = torch.zeros([1, max_seq]).int()
        pad_tokens[:, :input_lengths] = tokens
        tokens = pad_tokens
        text_mask = torch.zeros([1,max_seq]).int()
        text_mask[:, input_lengths:] = 1

        #==============================onnx bert model=========================================
        expand_text_mask = torch.unsqueeze(text_mask, 0).clone().unsqueeze_(0)
        expand_text_mask = torch.tile(expand_text_mask, (1, 1, 200, 1))
        input_2 = torch.where(expand_text_mask.bool(), -300, 0).float()

        if self.use_npu:
            npu_inputs = [tokens.int().numpy(), input_2.numpy()]
            d_en = self.npu_session0.forward(npu_inputs)[0]
            d_en = np.reshape(d_en, (1, 512, 200))
        else:
            ort_inputs = {'input1': tokens.int().numpy(), 'input2': input_2.numpy()}
            d_en = self.ort_session0.run(['out'], ort_inputs)[0]

        d_en = torch.tensor(d_en)
        s = ref_s[:, 128:]
        s = s.expand(max_seq, 1, 128)

        #==============================onnx predictor model==========================================
        if self.use_npu:
            npu_inputs1 = [d_en.numpy(), s.numpy()[None, : ,:]]
            d, duration = self.npu_session1.forward(npu_inputs1)
            d = np.reshape(d, (1, 200, 640))
            duration = np.reshape(duration, (1, 200))
        else:
            ort_inputs1 = {'input1': d_en.numpy(), 'input2': s.numpy()[None, : ]}
            d, duration = self.ort_session1.run(['out0', "out1"], ort_inputs1)

        d = torch.tensor(d)
        duration = torch.tensor(duration)
        d[:,input_lengths:,:] = 0
        duration[:, input_lengths:] = 0
        duration = duration / speed

        #==========================================================================
        pred_dur = torch.round(duration[:, :input_lengths]).clamp(min=1).long()  #[1,185]
        pred_aln_trg = torch.zeros(input_lengths.item(), pred_dur.sum().item()) #[500,sum]
        sum = pred_dur.sum().int().item()  #264
        c_frame = 0
        for i in range(input_lengths[0]):
            pred_aln_trg[i, c_frame:c_frame + pred_dur[0,i].item()] = 1
            c_frame += pred_dur[0,i].item()

        #==========================================================================
        out_length = 800
        new_pred_aln_trg = torch.zeros(max_seq,out_length)
        new_pred_aln_trg[:input_lengths.item(), :sum] = pred_aln_trg
        pred_aln_trg = new_pred_aln_trg.unsqueeze_(0)

        #===============================onnx text model===========================================
        if self.use_npu:
            npu_inputs2 = [d.numpy(), pred_aln_trg.numpy(), s[:1,:,:].numpy(), pad_tokens.int().numpy()]
            F0_pred, N_pred, asr = self.npu_session2.forward(npu_inputs2)
            F0_pred = np.reshape(F0_pred, (1, 1600))
            N_pred = np.reshape(N_pred, (1, 1600))
            asr = np.reshape(asr, (1, 1, 512, 800))
        else:
            ort_inputs2 = {'input1': d.numpy(), 'input2': pred_aln_trg.numpy(), 'input3': s[:1,:,:].numpy(), 'input4': pad_tokens.int().numpy()}
            F0_pred, N_pred, asr = self.ort_session2.run(['out0', "out1", "out2"], ort_inputs2)

        F0_pred = torch.tensor(F0_pred)
        N_pred = torch.tensor(N_pred)
        asr = torch.tensor(asr)

        #=================================decoder00/01===========================================
        if self.use_npu:
            decoder00_inputs = [asr.squeeze_(0).numpy(), F0_pred.numpy(), N_pred.numpy(), ref_s[:, :128].numpy()]
            x, f0, fn = self.npu_session3.forward(decoder00_inputs)
            x = np.reshape(x, (1, 512, 1600))
            f0 = np.reshape(f0, (1, 480000, 1))
            fn = np.reshape(fn, (1, 1600, 9))
        else:
            decoder00_inputs = {'input1': asr.squeeze_(0).numpy(), 'input2':F0_pred.numpy(), 'input3':N_pred.numpy(), 'input4':ref_s[:, :128].numpy()}
            x, f0, fn = self.ort_session3.run(['out0', "out1", "out2"], decoder00_inputs)

        x = torch.tensor(x).float()
        f0 = torch.tensor(f0).float()
        fn = torch.tensor(fn).float()
        fn = torch.cumsum(fn, dim=1)
        fn = torch.nn.functional.interpolate(fn.transpose(1, 2), scale_factor=300, mode="linear").transpose(1, 2)
        fn *= (300 * 2 * np.pi)
        sines = torch.sin(fn)
        sine_waves = sines * 0.1
        uv = (f0 > 10).type(torch.float32)
        noise_amp = uv * 0.003 + (1 - uv) * 0.1 * 0.33333
        sine_waves = sine_waves * uv + noise_amp

        if self.use_npu:
            decoder01_inputs = [sine_waves.numpy()]
            har_source = self.npu_session4.forward(decoder01_inputs)[0]
            har_source = np.reshape(har_source, (1, 480000))
        else:
            decoder01_inputs = {'input1': sine_waves.numpy()}
            har_source = self.ort_session4.run(['out0'], decoder01_inputs)[0]

        #=====================================onnx decoder1==================================================================
        har_source = torch.tensor(har_source[:, :sum * 600])  # [1,]
        x = x[:, :, :2 * sum]  # [1,512,4000]
        filter_length = 20
        hop_length = 5
        win_length = 20
        window = torch.tensor([0.0000, 0.0245, 0.0955, 0.2061, 0.3455, 0.5000, 0.6545, 0.7939, 0.9045,
                            0.9755, 1.0000, 0.9755, 0.9045, 0.7939, 0.6545, 0.5000, 0.3455, 0.2061,
                            0.0955, 0.0245])
        forward_transform = torch.stft(
            har_source,
            filter_length, hop_length, win_length, window=window, return_complex=True)
        har_spec = torch.abs(forward_transform)
        har_phase = torch.angle(forward_transform)
        valid_len = har_spec.shape[2]
        pad_x = torch.zeros(1, 512, out_length * 2)
        pad_x[:, :, :2 * sum] = x
        newhar_spec = torch.zeros(1, 11, 120 * out_length + 1)
        newhar_phase = torch.zeros(1, 11, 120 * out_length + 1)
        newhar_spec[:, :, :valid_len] = har_spec
        newhar_phase[:, :, :valid_len] = har_phase
        gather_index0 = torch.arange(20 * sum).unsqueeze_(0)
        gather_index1 = torch.arange(120 * sum).unsqueeze_(0)
        matmul_mat0 = torch.zeros([1, 256, 16000 - 20 * sum])
        matmul_mat1 = torch.zeros([1, 128, 96001 - 120 * sum])

        decoder1_inputs = {
            'input1': pad_x.numpy(),
            'input2': ref_s[:, :128].numpy(),
            'input3': newhar_spec.numpy(),
            'input4': newhar_phase.numpy(),
            'input5': gather_index0.numpy(),
            'input6': gather_index1.numpy(),
            'input7': matmul_mat0.numpy(),
            'input8': matmul_mat1.numpy(),
        }
        spec, phase = self.ort_session5.run(['out0', 'out1'], decoder1_inputs)

        spec = torch.tensor(spec[:, :, :valid_len])
        phase = torch.tensor(phase[:, :, :valid_len])
        inverse_transform = torch.istft(
            spec * torch.exp(phase * 1j),
            filter_length, hop_length, win_length, window=window)
        audio = inverse_transform.squeeze().cpu().numpy()
        end_time = time.perf_counter()
        print(f"Inference time: {end_time - start_time:.2f} seconds\n")
        return audio, pred_dur

    def forward(
        self,
        phonemes: str,
        ref_s: torch.FloatTensor,
        speed: float = 1,
        return_output: bool = False
    ) -> Union['KModel.Output', torch.FloatTensor]:
        input_ids = list(filter(lambda i: i is not None, map(lambda p: self.vocab.get(p), phonemes)))
        assert len(input_ids)+2 <= self.context_length, (len(input_ids)+2, self.context_length)
        input_ids = torch.LongTensor([[0, *input_ids, 0]]).to(self.device)
        ref_s = ref_s.to(self.device)
        audio, pred_dur = self.forward_with_tokens(input_ids, ref_s, speed)
        #audio = audio.squeeze().cpu()
        pred_dur = pred_dur.cpu() if pred_dur is not None else None
        return self.Output(audio=audio, pred_dur=pred_dur) if return_output else audio

    def clean(self):
        if self.use_npu:
            self.npu_session0.clean()
            self.npu_session1.clean()
            self.npu_session2.clean()
            self.npu_session3.clean()
            self.npu_session4.clean()
            self.ort_session5 = None
        else:
            self.ort_session0 = None
            self.ort_session1 = None
            self.ort_session2 = None
            self.ort_session3 = None
            self.ort_session4 = None
            self.ort_session5 = None
