import torch
import torch.nn as nn
import torch.nn.functional as F
import types

def get_head(task, backbone_channels, num_outputs, config=None, multiscale=True, fusion=False):
    """ Return the decoder head """
    head_type = config.MODEL.DECODER_HEAD.get(task, 'hrnet')

    if fusion:
        head_type = config.MODEL.FUSION_HEAD

    if head_type == 'hrnet':
        print(
            f"Using hrnet for task {task} with backbone channels {backbone_channels}")
        from models.seg_hrnet import HighResolutionHead
        return HighResolutionHead(backbone_channels, num_outputs)
    elif head_type == 'updecoder':
        print(f"Using updecoder for task {task}")
        from models.updecoder import Decoder
        return Decoder(backbone_channels, num_outputs, args=types.SimpleNamespace(**{
            'num_deconv': 3,
            'num_filters': [32, 32, 32],
            'deconv_kernels': [2, 2, 2]
        }))
    elif head_type == 'segformer':
        print(
            f"Using segformer for task {task} with {config.MODEL.SEGFORMER_CHANNELS} channels")
        from models.segformer import SegFormerHead
        return SegFormerHead(in_channels=backbone_channels, channels=config.MODEL.SEGFORMER_CHANNELS, num_classes=num_outputs)

    # 3x3 heads
    elif head_type == '3x3conv':
        print(
            f"Using 3x3conv for task {task} with backbone channels {backbone_channels}")
        from models.convheads import ConvHead3x3
        return ConvHead3x3(config, backbone_channels, num_outputs)

    else:
        if not multiscale:
            from models.aspp_single import DeepLabHead
        else:
            from models.aspp import DeepLabHead
        print(f"Using ASPP for task {task}")
        return DeepLabHead(backbone_channels, num_outputs)



class DecoderGroup(nn.Module):
    def __init__(self, tasks, num_outputs, channels, out_size, config, multiscale=True):
        super(DecoderGroup, self).__init__()
        self.tasks = tasks
        self.num_outputs = num_outputs
        self.channels = channels
        self.decoders = nn.ModuleDict()
        self.out_size = out_size
        self.multiscale = multiscale
        self.config = config

        for task in self.tasks:
            self.decoders[task] = get_head(task, self.channels, self.num_outputs[task], config=config, multiscale=self.multiscale)

    def forward(self, x, shared_prompt=None):

        result = {
            task: F.interpolate(self.decoders[task](
                x[task]), self.out_size, mode='bilinear')
            for task in self.tasks
        }
        return result


class Downsampler(nn.Module):
    def __init__(self, dims, channels, input_res, bias=False, enabled=True):
        """
        enabled : if enabled == False : just reshape input shape B L C -> B H W C -> B C H W
        """
        super(Downsampler, self).__init__()
        self.dims = dims
        self.input_res = input_res
        self.enabled = enabled
        if self.enabled:
            self.downsample_0 = torch.nn.Conv2d(
                dims[0], channels[0], 1, bias=bias)
            self.downsample_1 = torch.nn.Conv2d(
                dims[1], channels[1], 1, bias=bias)
            self.downsample_2 = torch.nn.Conv2d(
                dims[2], channels[2], 1, bias=bias)
            self.downsample_3 = torch.nn.Conv2d(
                dims[3], channels[3], 1, bias=bias)

    def forward(self, x):
        s_3 = x[3].view(-1, self.input_res[3],
                        self.input_res[3], self.dims[3]).permute(0, 3, 1, 2)

        s_2 = x[2].view(-1, self.input_res[2],
                        self.input_res[2], self.dims[2]).permute(0, 3, 1, 2)
        s_1 = x[1].view(-1, self.input_res[1],
                        self.input_res[1], self.dims[1]).permute(0, 3, 1, 2)
        s_0 = x[0].view(-1, self.input_res[0],
                        self.input_res[0], self.dims[0]).permute(0, 3, 1, 2)

        if self.enabled:
            return [
                self.downsample_0(s_0),
                self.downsample_1(s_1),
                self.downsample_2(s_2),
                self.downsample_3(s_3)
            ]
        else:
            return [
                s_0,
                s_1,
                s_2,
                s_3
            ]


class MultiTaskSwin(nn.Module):
    def __init__(self, encoder, config):
        super(MultiTaskSwin, self).__init__()

        self.backbone = encoder
        self.num_outputs = config.TASKS_CONFIG.ALL_TASKS.NUM_OUTPUT
        self.tasks = config.TASKS
        if hasattr(self.backbone, 'patch_embed'):
            patches_resolution = self.backbone.patch_embed.patches_resolution
            self.embed_dim = self.backbone.embed_dim
            num_layers = self.backbone.num_layers
            self.dims = [int((self.embed_dim * 2 ** ((i+1) if i < num_layers - 1 else i)))
                         for i in range(num_layers)]
            self.input_res = [patches_resolution[0] //
                              (2 ** ((i+1) if i < num_layers - 1 else i)) for i in range(num_layers)]
            self.window_size = self.backbone.layers[0].blocks[0].window_size
            self.img_size = self.backbone.patch_embed.img_size
        else:
            self.input_res = [28, 14, 7, 7]

            self.dims = [192, 384, 768, 768]
            self.window_size = config.MODEL.SWIN.WINDOW_SIZE
            self.img_size = config.DATA.IMG_SIZE

        self.channels = config.MODEL.DECODER_CHANNELS if config.MODEL.DECODER_DOWNSAMPLER else self.dims

        # MTLprompt
        self.mtlprompt = config.MODEL.MTLPROMPT

        if self.mtlprompt.ENABLED:
            if self.mtlprompt.DECODER_TYPE == "baseline":
                self.downsampler = nn.ModuleDict(
                    {task: Downsampler(dims=self.dims, channels=self.channels, input_res=self.input_res, bias=False) for
                     task in self.tasks})
            else:
                self.downsampler = Downsampler(dims=self.dims, channels=self.channels, input_res=self.input_res, bias=False, enabled=False)

        else:
            self.downsampler = Downsampler(
                dims=self.dims, channels=self.channels, input_res=self.input_res, bias=False)

        self.per_task_downsampler = config.MODEL.PER_TASK_DOWNSAMPLER
        print("Downsampler enabled:", config.MODEL.DECODER_DOWNSAMPLER)
        if config.MODEL.DECODER_DOWNSAMPLER:
            print("Decoder channels: ", self.channels)
            print("Per task downsampler: ", self.per_task_downsampler)
        if self.per_task_downsampler:
            self.downsampler = nn.ModuleDict({
                task: Downsampler(
                    dims=self.dims, channels=self.channels, input_res=self.input_res, bias=False, enabled=config.MODEL.DECODER_DOWNSAMPLER)
                for task in self.tasks
            })
        else:
            self.downsampler = Downsampler(
                dims=self.dims, channels=self.channels, input_res=self.input_res, bias=False)

        self.decoders = DecoderGroup(
            self.tasks, self.num_outputs, channels=self.channels, out_size=self.img_size, config=config, multiscale=True)

    def forward(self, x):
        # Get Shared Feature (Encoder output) here

        if self.mtlprompt.ENABLED and self.mtlprompt.DECODER_TYPE == "baseline": # Baseline
            x, x_downsample, shared_prompt = self.backbone(x)
            shared_ft = {
                task: [] for task in self.tasks
            }
            for tasks_shared_rep in x_downsample:
                for task, shared_rep in tasks_shared_rep.items():
                    shared_ft[task].append(shared_rep)
            for task in self.tasks:
                shared_ft[task] = self.downsampler[task](shared_ft[task])

            result = self.decoders(shared_ft)

        else :
            shared_representation = self.backbone(x)
            shared_ft = shared_representation

        return result

    def freeze_all(self):
        for param in self.parameters():
            param.requires_grad = False

    def unfreeze_all(self):
        for param in self.parameters():
            param.requires_grad = True

    def freeze_task(self, task):
        for param in self.decoders[task].parameters():
            param.requires_grad = False

    def unfreeze_task(self, task):
        for param in self.decoders[task].parameters():
            param.requires_grad = True

    def freeze_backbone(self):
        for param in self.backbone.parameters():
            param.requires_grad = False

    def unfreeze_backbone(self):
        for param in self.backbone.parameters():
            param.requires_grad = True
